"""Turn loop: parse user intent -> update state -> decide next step -> reply.

The controller alone decides which tool to call. The model only supplies an Intent; tool output is
sanitised and display-only. One top-level handler turns every exit into a user message and an
`exit` trace event.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from gharmitra.agent.approval import ApprovalError, Approvals, Values
from gharmitra.agent.budget import Budget, LimitExceeded
from gharmitra.agent.executor import Executor, Outcome
from gharmitra.agent.intents import IntentParser, Parsed
from gharmitra.agent.state import (
    OPTIONAL, REQUIRED, SEARCH_INPUTS, TERMINAL, BookingRef, FieldValue, Hold, Option, Phase, Session,
    SessionStore, Task, check_transition,
)
from gharmitra.agent.tools import Tools
from gharmitra.catalogue import Catalogue
from gharmitra.clock import Clock
from gharmitra.config import Limits
from gharmitra.db import Database
from gharmitra.llm.base import Entities, Intent, ModelClient
from gharmitra.llm.rules import RuleModel
from gharmitra.memory.store import MemoryStore, label
from gharmitra.mock_api.provider import ProviderAPI, SlotUnavailable
from gharmitra.money import format_inr, rupees
from gharmitra.trace import Redactor, TraceSink, Tracer, next_seq

log = logging.getLogger(__name__)

APPROVAL_MINUTES = 10  # cancel and reschedule; a book approval expires with its hold
MAX_OPTIONS = 3


@dataclass
class Deps:
    db: Database
    clock: Clock
    catalogue: Catalogue
    model: ModelClient
    fallback: ModelClient | None
    api_for: Callable[[str], ProviderAPI]
    sinks: list[TraceSink]
    limits: Limits = field(default_factory=Limits)
    redactor: Redactor = field(default_factory=Redactor)
    crash_points: set[str] = field(default_factory=set)


@dataclass
class Reply:
    session_id: str
    messages: list[str]
    phase: str
    approval: dict[str, Any] | None = None
    options: list[dict[str, Any]] = field(default_factory=list)
    exit: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


def fmt_slot(iso: str, tz: Any) -> str:
    d = datetime.fromisoformat(iso).astimezone(tz)
    return f"{d:%a} {d.day} {d:%b %H:%M}"


class Turn:
    """Everything one user message needs. Built fresh per turn."""

    def __init__(self, c: Controller, session: Session, tracer: Tracer) -> None:
        self.c = c
        self.s = session
        self.task = session.task
        self.tracer = tracer
        self.clock = c.deps.clock
        self.budget = Budget(c.deps.limits, self.clock)
        self.tools = Tools(c.deps.api_for(session.user_id), self.budget, tracer, self.clock)
        self.approvals = Approvals(c.deps.db, self.clock, tracer)
        self.executor = Executor(c.deps.db, self.approvals, self.clock, tracer, c.deps.limits, c.deps.crash_points)
        self.memory = MemoryStore(c.deps.db, self.clock, tracer)
        self.out: list[str] = []
        self.card: dict[str, Any] | None = None
        self.exit_reason: str | None = None
        self.message_id: int | None = None

    # --- small helpers -----------------------------------------------------------------

    @property
    def user(self) -> str:
        return self.s.user_id

    def say(self, text: str) -> None:
        self.out.append(text)

    def go(self, phase: Phase, reason: str) -> None:
        src = self.s.phase
        if src == phase:
            return
        check_transition(src, phase)
        self.s.phase = phase
        self.tracer.emit("state_transition", src=src.value, dst=phase.value, reason=reason)

    def exit(self, reason: str) -> None:
        self.exit_reason = reason
        self.tracer.emit("exit", reason=reason, phase=self.s.phase.value, budget=self.budget.snapshot())

    def slot(self, iso: str) -> str:
        return fmt_slot(iso, self.clock.tz)

    def label(self, service: str | None) -> str:
        return self.c.deps.catalogue.service_label(service or "").lower()


class Controller:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self.sessions = SessionStore(deps.db, deps.clock)
        self._tracers: dict[str, Tracer] = {}

    # --- sessions -------------------------------------------------------------------------

    def start_session(self, user_id: str, session_id: str | None = None) -> str:
        return self.sessions.create(user_id, session_id).id

    def tracer(self, session_id: str) -> Tracer:
        if session_id not in self._tracers:
            self._tracers[session_id] = Tracer(
                session_id, self.deps.sinks, self.deps.clock, self.deps.redactor,
                start_seq=next_seq(self.deps.db, session_id),
            )
        return self._tracers[session_id]

    def parser(self, tracer: Tracer) -> IntentParser:
        d = self.deps
        return IntentParser(d.model, d.fallback, d.catalogue, tracer, d.limits.min_confidence)

    # --- the turn -------------------------------------------------------------------------------

    def handle(self, session_id: str, text: str, approval_id: str | None = None) -> Reply:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session {session_id}")
        tracer = self.tracer(session_id)
        t = Turn(self, session, tracer)
        try:
            t.message_id = self.sessions.add_message(session_id, "user", text)
            tracer.emit("user_msg", text=text, approval_id=approval_id)
            session.turns += 1
            t.budget.check_turns(session.turns)
            self._reconcile(t)
            parsed = self.parser(tracer).parse(
                text, today=t.clock.now().date(), phase=session.phase.value, known_fields=t.task.known_fields(),
                awaiting=t.task.awaiting, budget=t.budget, options_shown=len(t.task.options),
            )
            i = parsed.intent
            tracer.emit("intent", act=i.act, goal=i.goal, entities=i.entities.model_dump(mode="json", exclude_defaults=True),
                        option=i.option, memory=i.memory.model_dump() if i.memory else None, remember=i.remember,
                        confidence=i.confidence, usable=parsed.usable, problems=parsed.problems,
                        adapter=parsed.adapter, fallback=parsed.fallback)
            self._apply(t, parsed, approval_id)
        except LimitExceeded as exc:
            t.say(self._limit_message(t, exc.reason))
            t.exit(exc.reason)
        except Exception:  # every other failure still ends with a clear message
            log.exception("turn failed")
            t.say("Sorry, something went wrong on my side, so I stopped. " + self._known_state(t))
            t.exit("unknown_error")
        tracer.emit("budget", **t.budget.snapshot())
        self.sessions.save(session)
        for line in t.out:
            self.sessions.add_message(session_id, "agent", line)
            tracer.emit("agent_msg", text=line)
        return Reply(session_id, t.out, session.phase.value, t.card,
                     [o.model_dump() for o in t.task.options] if session.phase == Phase.PROPOSING else [],
                     t.exit_reason)

    # --- exits ---------------------------------------------------------------------------------

    def _known_state(self, t: Turn) -> str:
        open_ops = t.executor.open_operations(t.s.id)
        if open_ops:
            return "The booking is unconfirmed; I'll check its status again on your next message."
        a = t.approvals.get(t.task.approval_id)
        if a is not None and a["status"] == "pending":
            return "Your approval request is still open: " + a["summary"]
        return "Nothing was booked or changed."

    def _limit_message(self, t: Turn, reason: str) -> str:
        lim = self.deps.limits
        text = {
            "limit_turns": f"This conversation has reached its limit of {lim.turns_per_session} messages. "
                           "Please start a new session.",
            "limit_model_calls": "I couldn't work out your message within my limit of "
                                 f"{lim.model_calls_per_turn} model calls.",
            "limit_tool_calls": f"I stopped after {lim.tool_calls_per_turn} provider calls, my limit for one message.",
            "limit_time": f"I stopped because this took longer than {lim.seconds_per_turn:.0f} seconds.",
        }[reason]
        return f"{text} {self._known_state(t)}"

    # --- restart reconciliation -------------------------------------------------------------------

    def _reconcile(self, t: Turn) -> None:
        """Before new input, settle every operation left started or unknown (restart, timeout)."""
        for op in t.executor.open_operations(t.s.id):
            a = t.approvals.get(op["approval_id"])
            values = self._values(t, a)
            if values is None:
                continue
            t.tracer.emit("reconcile", op_key=op["op_key"], status=op["status"])
            outcome = t.executor.resume(op, values, t.tools)
            self._after_outcome(t, outcome, a)

    # --- dispatch -------------------------------------------------------------------------------------

    def _apply(self, t: Turn, parsed: Parsed, approval_id: str | None) -> None:
        i = parsed.intent
        if approval_id is not None:
            if not self._approval_is_current(t, approval_id):
                t.say("That approval is no longer valid, so I didn't act on it. " + self._current_prompt(t))
                return
            if i.act not in ("confirm", "decline"):
                i = Intent(act="confirm")
        if not parsed.usable:
            t.say(self._clarify(t))
            return
        if parsed.problems:
            t.say(self._problem_text(t, parsed.problems))
        handler = {
            "memory": self._on_memory,
            "status": self._on_status,
            "confirm": self._on_confirm,
            "decline": self._on_decline,
            "select": self._on_select,
            "request": self._on_request,
            "inform": self._on_inform,
        }[i.act]
        handler(t, i)

    def _approval_is_current(self, t: Turn, approval_id: str) -> bool:
        a = t.approvals.get(approval_id)
        return (
            a is not None
            and approval_id == t.task.approval_id
            and a["session_id"] == t.s.id
            and a["status"] == "pending"
            and t.s.phase == Phase.AWAITING_APPROVAL
        )

    def _current_prompt(self, t: Turn) -> str:
        a = t.approvals.get(t.task.approval_id)
        if t.s.phase == Phase.AWAITING_APPROVAL and a is not None and a["status"] == "pending":
            self._show_card(t, a)
            return a["summary"] + " (yes/no)"
        return ""

    def _clarify(self, t: Turn) -> str:
        if t.task.awaiting == "select" and t.task.options:
            return f"Please pick an option (1–{len(t.task.options)}), or tell me what to change."
        if t.task.awaiting == "confirm":
            return "Please reply yes to confirm or no to cancel."
        return ("Sorry, I didn't catch that. I can book, reschedule or cancel home services, for example: "
                "\"Find a plumber in Koramangala tomorrow morning\".")

    def _problem_text(self, t: Turn, problems: dict[str, str]) -> str:
        cat = self.deps.catalogue
        parts = []
        if "area" in problems:
            parts.append(f"I don't cover \"{problems['area']}\" yet. Areas: {', '.join(cat.areas)}.")
        if "service" in problems:
            labels = ", ".join(cat.service_label(s).lower() for s in cat.services)
            parts.append(f"I can't book \"{problems['service']}\". Services: {labels}.")
        if problems.get("date") == "past":
            parts.append("That date has already passed.")
        if problems.get("date") == "too_far":
            parts.append("I can only book up to 30 days ahead.")
        if "time" in problems:
            parts.append("That time window is empty.")
        return " ".join(parts)

    # --- acts -------------------------------------------------------------------------------------------

    def _on_request(self, t: Turn, i: Intent) -> None:
        task = t.task
        in_progress = task.goal is not None and t.s.phase not in TERMINAL
        if not in_progress or task.goal != i.goal:
            if in_progress:
                t.approvals.void(task.approval_id, "new_request")
            self._new_task(t, i.goal or "book")
        if i.target and i.target.service and t.task.goal in ("reschedule", "cancel"):
            t.task.fields["service"] = FieldValue(value=i.target.service)
        self._merge(t, i.entities, i.remember)
        self._advance(t)

    def _on_inform(self, t: Turn, i: Intent) -> None:
        if t.task.goal is None or t.s.phase in TERMINAL:
            if not i.entities.given():
                t.say(self._clarify(t))
                return
            self._new_task(t, "book")
        self._merge(t, i.entities, i.remember)
        self._advance(t)

    def _new_task(self, t: Turn, goal: str) -> None:
        if t.s.phase != Phase.COLLECTING:
            t.go(Phase.COLLECTING, "new_task")
        t.s.task = t.task = Task(goal=goal)  # type: ignore[arg-type]

    def _on_select(self, t: Turn, i: Intent) -> None:
        task = t.task
        n = i.option or 0
        if task.awaiting == "pick_booking" and task.candidates:
            if not 1 <= n <= len(task.candidates):
                t.say(f"Please pick a booking between 1 and {len(task.candidates)}.")
                return
            task.target = task.candidates[n - 1]
            task.candidates = []
            self._advance(t)
            return
        if t.s.phase not in (Phase.PROPOSING, Phase.AWAITING_APPROVAL) or not task.options:
            t.say("There are no options to pick from yet. " + self._current_prompt(t))
            return
        if not 1 <= n <= len(task.options):
            t.say(f"Please pick an option between 1 and {len(task.options)}.")
            return
        if task.approval_id:
            t.approvals.void(task.approval_id, "selection_changed")
            task.approval_id = None
            task.hold = None
        task.chosen = task.options[n - 1]
        if task.goal == "reschedule":
            self._propose_reschedule(t)
        else:
            self._hold(t)

    def _on_confirm(self, t: Turn, i: Intent) -> None:
        task = t.task
        if task.awaiting == "use_inferred" and task.pending_fact:
            key = task.pending_fact["key"]
            t.memory.confirm(t.user, key)
            task.fields["provider"] = FieldValue(
                value={"id": task.pending_fact["value"], "name": task.pending_fact["name"], "strict": True},
                source="memory")
            task.pending_fact = None
            task.awaiting = None
            t.say(f"OK, I'll look for {task.value('provider')['name']} and remember them as your "
                  f"{label(key)}.")
            self._advance(t)
            return
        if t.s.phase == Phase.AWAITING_APPROVAL and task.approval_id:
            self._grant_and_execute(t)
            return
        if task.result and t.s.phase == Phase.DONE:
            t.say(f"That's already done (booking {task.result['booking_id']}). No new action taken.")
            return
        t.say("There's nothing waiting for your confirmation. " + self._current_prompt(t))

    def _on_decline(self, t: Turn, i: Intent) -> None:
        task = t.task
        if task.awaiting == "use_inferred" and task.pending_fact:
            task.declined_facts.append(task.pending_fact["key"])
            task.pending_fact = None
            task.awaiting = None
            task.fields["provider"] = FieldValue(value=None)
            t.say("OK, I won't use them.")
            self._advance(t)
            return
        if t.s.phase == Phase.AWAITING_APPROVAL and task.approval_id:
            t.approvals.void(task.approval_id, "user_declined")
            task.approval_id = None
            task.hold = None
            task.awaiting = None
            t.go(Phase.ABORTED, "user_declined")
            t.say("OK, I haven't done anything. Tell me if you'd like to change something.")
            t.exit("user_cancelled")
            return
        if task.goal and t.s.phase not in TERMINAL:
            t.approvals.void(task.approval_id, "user_declined")
            t.go(Phase.ABORTED, "user_declined")
            t.say("OK, I've stopped. Nothing was booked or changed.")
            t.exit("user_cancelled")
            return
        t.say("OK.")

    # --- merging fields -----------------------------------------------------------------------------------

    def _merge(self, t: Turn, e: Entities, remember: bool) -> None:
        task = t.task
        incoming: dict[str, Any] = {}
        if e.service:
            incoming["service"] = e.service
        if e.area:
            incoming["area"] = e.area
        if e.date:
            incoming["date"] = {"from": e.date.from_.isoformat(), "to": e.date.to.isoformat()}
        if e.time and (e.time.earliest or e.time.latest):
            incoming["time"] = {"earliest": e.time.earliest.strftime("%H:%M") if e.time.earliest else None,
                                "latest": e.time.latest.strftime("%H:%M") if e.time.latest else None}
        if e.budget_max is not None:
            incoming["budget_max"] = rupees(e.budget_max)
        if e.provider and e.provider.same_as_last:
            incoming["provider"] = {"same_as_last": True}
        changed = [k for k, v in incoming.items() if task.known(k) and task.value(k) != v]
        for k, v in incoming.items():
            task.fields[k] = FieldValue(value=v, source="user")
        if e.no_preference:
            for k in OPTIONAL:
                task.fields.setdefault(k, FieldValue(value=None, source="user"))
            task.optional_asked = True
        if changed:
            t.tracer.emit("correction", fields=changed)
            if set(changed) & SEARCH_INPUTS:
                self._invalidate(t, "correction")
        if remember:
            self._remember(t, incoming)

    def _invalidate(self, t: Turn, reason: str) -> None:
        """A changed search input clears only its dependants: options, choice, hold and approval."""
        task = t.task
        t.approvals.void(task.approval_id, reason)
        task.approval_id = None
        task.hold = None
        task.chosen = None
        task.options = []
        task.excluded = []
        if t.s.phase in (Phase.SEARCHING, Phase.PROPOSING, Phase.AWAITING_APPROVAL, Phase.HOLDING):
            t.go(Phase.COLLECTING, reason)

    def _remember(self, t: Turn, incoming: dict[str, Any]) -> None:
        saved = []
        if "area" in incoming:
            t.memory.state(t.user, "area", incoming["area"])
            saved.append("area")
        if incoming.get("time", {}).get("earliest"):
            t.memory.state(t.user, "time_earliest", incoming["time"]["earliest"])
            saved.append("earliest time")
        if "budget_max" in incoming:
            t.memory.state(t.user, "budget_max", str(incoming["budget_max"] // 100))
            saved.append("usual budget")
        if saved:
            t.say(f"I'll remember your {' and '.join(saved)}.")

    # --- the state machine ----------------------------------------------------------------------------------

    def _advance(self, t: Turn) -> None:
        goal = t.task.goal
        if goal == "book":
            self._advance_book(t)
        elif goal == "reschedule":
            self._advance_reschedule(t)
        elif goal == "cancel":
            self._advance_cancel(t)

    def _apply_memory(self, t: Turn) -> None:
        task = t.task
        if task.memory_applied:
            return
        task.memory_applied = True
        used = []
        area = t.memory.stated(t.user, "area")
        if area and not task.known("area"):
            task.fields["area"] = FieldValue(value=area, source="memory")
            used.append(f"area: {area}")
        earliest = t.memory.stated(t.user, "time_earliest")
        if earliest and not task.known("time"):
            task.fields["time"] = FieldValue(value={"earliest": earliest, "latest": None}, source="memory")
            used.append(f"earliest time: {earliest}")
        budget = t.memory.stated(t.user, "budget_max")
        if budget and not task.known("budget_max"):
            task.fields["budget_max"] = FieldValue(value=rupees(int(budget)), source="memory")
            used.append(f"budget: {format_inr(rupees(int(budget)))}")
        if used:
            t.say("Using your saved " + ", ".join(used) + ".")

    def _provider_from_memory(self, t: Turn) -> bool:
        """Stated preferred provider → preference. Inferred → ask first. Returns True if it asked."""
        task = t.task
        if task.pending_fact:
            t.say(f"Should I use {task.pending_fact['name']} again? (yes/no)")
            return True
        service = task.value("service")
        if not service or task.known("provider"):
            return False
        key = f"provider:{service}"
        if key in task.declined_facts:
            return False
        fact = t.memory.get(t.user, key)
        if fact is None:
            fact = self._infer_provider(t, service)
        if fact is None or fact["status"] == "forgotten":
            return False
        name = self._provider_name(t, fact["value"])
        if fact["status"] == "stated":
            task.fields["provider"] = FieldValue(value={"id": fact["value"], "name": name, "strict": False},
                                                 source="memory")
            t.say(f"Using your saved {label(key)}: {name}.")
            return False
        task.pending_fact = {"key": key, "value": fact["value"], "name": name}
        task.awaiting = "use_inferred"
        t.say(f"You've booked {name} more than once for {t.label(service)}. Use them again? (yes/no)")
        return True

    def _infer_provider(self, t: Turn, service: str) -> dict | None:
        bookings = t.tools.list_bookings(user_id=t.user)
        counts = Counter(b["provider_id"] for b in bookings
                         if b["service"] == service and b["status"] in ("completed", "booked"))
        if not counts:
            return None
        provider_id, n = counts.most_common(1)[0]
        if n < 2:
            return None
        if t.memory.infer(t.user, f"provider:{service}", provider_id):
            return t.memory.get(t.user, f"provider:{service}")
        return None

    def _provider_name(self, t: Turn, provider_id: str) -> str:
        name = self.deps.db.scalar(
            "select provider_name from bookings where provider_id = %s order by slot_start desc limit 1", (provider_id,)
        )
        return name or provider_id

    def _resolve_same_as_last(self, t: Turn) -> None:
        task = t.task
        p = task.value("provider")
        service = task.value("service")
        if not (isinstance(p, dict) and p.get("same_as_last")) or not service:
            return
        past = [b for b in t.tools.list_bookings(user_id=t.user)
                if b["service"] == service and b["status"] in ("completed", "booked")]
        if not past:
            task.fields["provider"] = FieldValue(value=None)
            t.say(f"I couldn't find a previous {t.label(service)} booking, so I'll show all providers.")
            return
        last = max(past, key=lambda b: b["slot"])
        task.fields["provider"] = FieldValue(
            value={"id": last["provider_id"], "name": last["provider_name"], "strict": True}, source="user")
        t.say(f"Last time you booked {last['provider_name']}.")

    def _advance_book(self, t: Turn) -> None:
        task = t.task
        self._apply_memory(t)
        self._resolve_same_as_last(t)
        if self._provider_from_memory(t):
            return
        missing = [k for k in REQUIRED["book"] if task.value(k) is None]
        if missing:
            task.awaiting = "required"
            t.say(self._ask_required(t, missing))
            return
        open_optional = [k for k in OPTIONAL if not task.known(k)]
        if open_optional and not task.optional_asked:
            task.optional_asked = True
            task.awaiting = "optional"
            names = {"time": "time of day", "budget_max": "budget", "provider": "provider"}
            t.say(f"Any preferred {', '.join(names[k] for k in open_optional)}? Say \"no preference\" to skip.")
            return
        self._search(t)

    def _ask_required(self, t: Turn, missing: list[str]) -> str:
        questions = {"service": "What service do you need (plumber, electrician, AC servicing or cleaning)?",
                     "area": "Which area are you in?", "date": "Which day or days work for you?"}
        return " ".join(questions[k] for k in missing)

    def _search(self, t: Turn) -> None:
        task = t.task
        t.go(Phase.SEARCHING, "fields_complete")
        if task.approval_id:
            t.approvals.void(task.approval_id, "new_search")
            task.approval_id = None
            task.hold = None
        d = task.value("date")
        results = t.tools.search_providers(service=task.value("service"), area=task.value("area"),
                                           date_range=(d["from"], d["to"]))
        window = task.value("time") or {}
        budget = task.value("budget_max")
        pref = task.value("provider") if isinstance(task.value("provider"), dict) else None
        candidates = []
        for p in results:
            if pref and pref.get("strict") and p["id"] != pref.get("id"):
                continue
            price = rupees(p["price"])
            if budget is not None and price > budget:
                continue
            slots = [s for s in p["free_slots"] if self._in_window(t, s, window) and f"{p['id']}|{s}" not in task.excluded]
            if slots:
                candidates.append((p, price, slots[0]))
        candidates.sort(key=lambda c: (not (pref and c[0]["id"] == pref.get("id")), -c[0]["rating"], c[1]))
        options = []
        for p, price, slot in candidates[:MAX_OPTIONS]:
            details = t.tools.get_provider(provider_id=p["id"])
            options.append(Option(provider_id=p["id"], provider_name=p["name"], rating=p["rating"], price=price,
                                  slot=slot, reviews=[r["text"] for r in details["reviews"][:2]]))
        task.options = options
        if not options:
            t.go(Phase.COLLECTING, "no_options")
            task.awaiting = "required"
            t.say("I couldn't find anything that matches. Try another day, time or budget.")
            return
        self._show_options(t, "Here are the best matches:")

    def _in_window(self, t: Turn, slot: str, window: dict) -> bool:
        local = datetime.fromisoformat(slot).astimezone(t.clock.tz).strftime("%H:%M")
        if window.get("earliest") and local < window["earliest"]:
            return False
        if window.get("latest") and local >= window["latest"]:
            return False
        return True

    def _show_options(self, t: Turn, intro: str) -> None:
        task = t.task
        t.go(Phase.PROPOSING, "options_ready")
        task.awaiting = "select"
        lines = [f"{intro}"]
        for n, o in enumerate(task.options, 1):
            lines.append(f"{n}. {o.provider_name}  ★{o.rating}  {t.slot(o.slot)}  {format_inr(o.price)}")
            for r in o.reviews:
                lines.append(f"     \"{r}\"" if not r.startswith("[") else f"     {r}")
        lines.append("Pick a number.")
        t.say("\n".join(lines))

    # --- hold and approval ----------------------------------------------------------------------------------

    def _hold(self, t: Turn, note: str = "") -> None:
        task = t.task
        o = task.chosen
        assert o is not None
        t.go(Phase.HOLDING, "option_selected")
        try:
            h = t.tools.hold_slot(provider_id=o.provider_id, slot=o.slot)
        except SlotUnavailable:
            task.excluded.append(f"{o.provider_id}|{o.slot}")
            task.options = [x for x in task.options if x != o]
            task.chosen = None
            if task.options:
                self._show_options(t, "That slot is no longer available. Remaining options:")
            else:
                t.go(Phase.PROPOSING, "slot_unavailable")
                self._search(t)
            return
        quoted = rupees(h["quoted_price"])
        budget = task.value("budget_max")
        if budget is not None and quoted > budget:
            task.excluded.append(f"{o.provider_id}|{o.slot}")
            task.options = [x for x in task.options if x != o]
            task.chosen = None
            if not task.options:
                t.say(f"{o.provider_name} quoted {format_inr(quoted)}, above your budget of {format_inr(budget)}.")
                t.go(Phase.PROPOSING, "over_budget")
                self._search(t)
                return
            self._show_options(t, f"{o.provider_name} quoted {format_inr(quoted)}, above your budget of "
                                  f"{format_inr(budget)}. Pick another option:")
            return
        task.hold = Hold(hold_id=h["hold_id"], provider_id=o.provider_id, provider_name=o.provider_name, slot=o.slot,
                         quoted_price=quoted, expires_at=h["expires_at"])
        price_note = "" if quoted == o.price else f" (listed at {format_inr(o.price)})"
        summary = f"Book {o.provider_name}, {t.slot(o.slot)}, {format_inr(quoted)} (quoted)."
        values = self._values_for(t, "book")
        self._create_approval(t, values, o.provider_name, summary, datetime.fromisoformat(h["expires_at"]))
        t.say(f"{note + ' ' if note else ''}Holding the slot. The provider quoted {format_inr(quoted)}{price_note}.\n"
              f"{summary} Confirm? (yes/no)")

    def _create_approval(self, t: Turn, values: Values, provider_name: str, summary: str, expires: datetime) -> None:
        a = t.approvals.create(session_id=t.s.id, user_id=t.user, values=values, provider_name=provider_name,
                               summary=summary, expires_at=expires)
        t.task.approval_id = a["id"]
        t.task.awaiting = "confirm"
        t.go(Phase.AWAITING_APPROVAL, "approval_shown")
        self._show_card(t, a)

    def _show_card(self, t: Turn, a: dict) -> None:
        t.card = {"approval_id": a["id"], "action": a["action"], "summary": a["summary"],
                  "amount": format_inr(a["amount"]), "expires_at": a["expires_at"].isoformat()}

    def _values_for(self, t: Turn, action: str) -> Values:
        """The values about to be sent, computed from the task (never from the approval row)."""
        task = t.task
        meta = {"user_id": t.user}
        if action == "book":
            h = task.hold
            assert h is not None
            meta.update(provider_name=h.provider_name, service=task.value("service"), area=task.value("area"))
            return Values("book", h.provider_id, h.slot, h.quoted_price, None, h.hold_id, meta)
        b = task.target
        assert b is not None
        if action == "cancel":
            return Values("cancel", b.provider_id, b.slot, b.price or 0, b.id, None, {**meta, "fee": 0})
        assert task.chosen is not None
        return Values("reschedule", b.provider_id, task.chosen.slot, b.price or 0, b.id, None, meta)

    def _values(self, t: Turn, a: dict | None) -> Values | None:
        try:
            return self._values_for(t, a["action"]) if a else None
        except AssertionError:
            return None

    def _grant_and_execute(self, t: Turn) -> None:
        task = t.task
        a = t.approvals.get(task.approval_id)
        if a is None or a["status"] != "pending":
            t.say("There's nothing waiting for your confirmation.")
            return
        if t.approvals.is_expired(a):
            t.approvals.expire(a["id"])
            task.approval_id = None
            self._renew(t, a, "That approval expired.")
            return
        granted = t.approvals.grant(a["id"], t.message_id)  # only here: a user turn, approval displayed
        if granted is None:
            t.say("That approval is no longer valid. " + self._current_prompt(t))
            return
        t.go(Phase.EXECUTING, "approval_granted")
        values = self._values_for(t, a["action"])
        try:
            outcome = t.executor.execute(a["id"], values, t.tools, t.s.id)
        except ApprovalError as exc:
            t.say(f"I couldn't go ahead: the approval check failed ({exc.reason}). Nothing was changed.")
            t.go(Phase.FAILED, f"gate_{exc.reason}")
            t.exit("unknown_error")
            return
        self._after_outcome(t, outcome, a)

    def _renew(self, t: Turn, old: dict, note: str) -> None:
        """A new approval for the same choice: re-hold for book, recompute for the others."""
        task = t.task
        if old["action"] == "book" and task.chosen:
            task.hold = None
            t.go(Phase.HOLDING, "approval_expired")
            self._hold(t, note=note + " I held the slot again.")
        elif old["action"] == "reschedule" and task.chosen:
            t.say(note)
            self._propose_reschedule(t)
        elif old["action"] == "cancel" and task.target:
            t.say(note)
            self._propose_cancel(t)

    def _after_outcome(self, t: Turn, out: Outcome, a: dict) -> None:
        task = t.task
        if t.s.phase in (Phase.AWAITING_APPROVAL, Phase.FAILED):
            if out.status == "unknown" and t.s.phase == Phase.FAILED:
                pass  # still unknown after an earlier retries_exhausted
            else:
                t.go(Phase.EXECUTING, "reconcile")  # resumed after a restart or an unconfirmed booking
        if out.status == "succeeded":
            t.go(Phase.VERIFYING, "call_returned")
            t.go(Phase.DONE, "verified")
            summary = self._success_text(t, out, a)
            task.result = {"booking_id": out.booking_id, "summary": summary, "approval_id": out.approval_id}
            task.awaiting = None
            task.approval_id = None
            prefix = "Checked after the interruption: " if out.resumed else ""
            t.say(prefix + summary)
            t.exit("completed")
            return
        if out.status == "unknown":
            if t.s.phase == Phase.EXECUTING:
                t.go(Phase.FAILED, "retries_exhausted")
            t.say("I couldn't confirm whether that went through. The booking is unconfirmed; "
                  "I'll check its status again on your next message.")
            t.exit("retries_exhausted")
            return
        # definite failure: the approval is already voided or expired
        reason = out.error or "failed"
        task.approval_id = None
        if reason in ("slot_taken", "slot_conflict"):
            if task.chosen:
                task.excluded.append(f"{task.chosen.provider_id}|{task.chosen.slot}")
            task.hold = None
            task.chosen = None
            what = "That slot was just taken" if reason == "slot_taken" else "That time conflicts with the provider's schedule"
            if task.goal == "reschedule":
                t.go(Phase.PROPOSING, reason)
                t.say(f"{what}. Your booking is unchanged.")
                self._reschedule_options(t)
            else:
                t.go(Phase.SEARCHING, reason)
                t.say(f"{what}, so nothing was booked.")
                self._search(t)
            return
        if reason in ("hold_expired", "approval_expired"):
            t.go(Phase.HOLDING, reason)
            self._renew(t, a, "The hold expired before the booking went through, so nothing was booked.")
            return
        t.go(Phase.FAILED, reason)
        t.say(f"That didn't go through ({reason}). Nothing was changed.")
        t.exit("unknown_error")

    def _success_text(self, t: Turn, out: Outcome, a: dict) -> str:
        when = t.slot(a["slot_start"].isoformat())
        if out.action == "book":
            return f"Booked and verified: {a['provider_name']}, {when}. Booking id {out.booking_id}."
        if out.action == "cancel":
            return (f"Cancelled and verified: {a['provider_name']}, {when}. "
                    f"Refund {format_inr(a['amount'])}. Booking id {out.booking_id}.")
        return f"Rescheduled and verified: {a['provider_name']} is now {when}. Booking id {out.booking_id}."

    # --- reschedule and cancel ---------------------------------------------------------------------------------

    def _resolve_target(self, t: Turn) -> bool:
        """Find the booking to change. Returns True when resolved; otherwise it asked or gave up."""
        task = t.task
        if task.target:
            return True
        service = task.value("service")
        now = t.clock.now()
        upcoming = [b for b in t.tools.list_bookings(user_id=t.user)
                    if b["status"] == "booked" and datetime.fromisoformat(b["slot"]) > now
                    and (service is None or b["service"] == service)]
        refs = [self._booking_ref(b) for b in upcoming]
        if not refs:
            what = f"{t.label(service)} " if service else ""
            t.say(f"I couldn't find an upcoming {what}booking to change.")
            t.go(Phase.ABORTED, "no_target")
            t.exit("completed")
            return False
        if len(refs) > 1:
            task.candidates = refs
            task.awaiting = "pick_booking"
            lines = ["Which booking?"] + [f"{n}. {r.provider_name} ({t.label(r.service)}), {t.slot(r.slot)}"
                                          for n, r in enumerate(refs, 1)]
            t.say("\n".join(lines))
            return False
        task.target = refs[0]
        return True

    def _booking_ref(self, b: dict) -> BookingRef:
        copy = self.deps.db.one("select price, area from bookings where id = %s", (b["id"],))
        return BookingRef(id=b["id"], provider_id=b["provider_id"], provider_name=b["provider_name"],
                          service=b["service"], slot=b["slot"], status=b["status"],
                          price=copy["price"] if copy else None, area=copy["area"] if copy else None)

    def _advance_reschedule(self, t: Turn) -> None:
        task = t.task
        if not self._resolve_target(t):
            return
        if task.value("date") is None:
            task.awaiting = "required"
            t.say(f"Which day should I move your {t.label(task.target.service)} booking to?")
            return
        self._reschedule_options(t)

    def _reschedule_options(self, t: Turn) -> None:
        task = t.task
        b = task.target
        assert b is not None
        area = b.area or task.value("area") or t.memory.stated(t.user, "area")
        if not area:
            task.awaiting = "required"
            t.say("Which area is the booking in?")
            return
        d = task.value("date")
        results = t.tools.search_providers(service=b.service, area=area, date_range=(d["from"], d["to"]))
        mine = next((p for p in results if p["id"] == b.provider_id), None)
        window = task.value("time") or {}
        slots = [s for s in (mine["free_slots"] if mine else [])
                 if s != b.slot and self._in_window(t, s, window) and f"{b.provider_id}|{s}" not in task.excluded]
        task.options = [Option(provider_id=b.provider_id, provider_name=b.provider_name, rating=mine["rating"],
                               price=b.price or rupees(mine["price"]), slot=s) for s in slots[:MAX_OPTIONS]] if mine else []
        if not task.options:
            if t.s.phase != Phase.COLLECTING:
                t.go(Phase.COLLECTING, "no_slots")
            task.awaiting = "required"
            t.say(f"{b.provider_name} has no free slots then. Try another day.")
            return
        self._show_options(t, f"{b.provider_name} can do these times (now {t.slot(b.slot)}):")

    def _propose_reschedule(self, t: Turn) -> None:
        task = t.task
        b, o = task.target, task.chosen
        assert b is not None and o is not None
        values = self._values_for(t, "reschedule")
        summary = (f"Move {b.provider_name} ({t.label(b.service)}) from {t.slot(b.slot)} to {t.slot(o.slot)}, "
                   f"price {format_inr(values.amount)} unchanged.")
        self._create_approval(t, values, b.provider_name, summary, t.clock.now() + timedelta(minutes=APPROVAL_MINUTES))
        t.say(f"{summary} Confirm? (yes/no)")

    def _advance_cancel(self, t: Turn) -> None:
        if not self._resolve_target(t):
            return
        self._propose_cancel(t)

    def _propose_cancel(self, t: Turn) -> None:
        b = t.task.target
        assert b is not None
        values = self._values_for(t, "cancel")
        summary = (f"Cancel {b.provider_name} ({t.label(b.service)}), {t.slot(b.slot)}. "
                   f"Refund {format_inr(values.amount)}.")
        self._create_approval(t, values, b.provider_name, summary, t.clock.now() + timedelta(minutes=APPROVAL_MINUTES))
        t.say(f"{summary} Confirm? (yes/no)")

    # --- memory and status -----------------------------------------------------------------------------------------

    def _on_memory(self, t: Turn, i: Intent) -> None:
        m = i.memory
        assert m is not None
        if m.op == "view":
            facts = t.memory.all(t.user)
            if not facts:
                t.say("I don't have any saved preferences for you.")
                return
            lines = ["Here's what I remember:"]
            for f in facts:
                value = self._provider_name(t, f["value"]) if f["key"].startswith("provider:") else f["value"]
                note = "you told me" if f["status"] == "stated" else "inferred, I'll ask before using it"
                lines.append(f"- {label(f['key'])}: {value} ({note})")
            t.say("\n".join(lines))
            return
        key = m.key
        if not key:
            t.say("Which preference? I keep your area, earliest time, budget and preferred providers.")
            return
        if m.op == "forget":
            forgotten = t.memory.forget(t.user, key)
            task = t.task
            field_key = {"area": "area", "time_earliest": "time", "budget_max": "budget_max"}.get(key, "provider")
            if task.fields.get(field_key) and task.fields[field_key].source == "memory":
                task.fields.pop(field_key)
                self._invalidate(t, "memory_forgotten")
            t.say(f"Done, I've forgotten your {label(key)}." if forgotten else f"I didn't have your {label(key)} saved.")
            return
        value = self._normalise_fact(key, m.value)
        if value is None:
            t.say(f"I couldn't use \"{m.value}\" as your {label(key)}.")
            return
        t.memory.correct(t.user, key, value)
        t.say(f"Updated your {label(key)} to {value}.")

    def _normalise_fact(self, key: str, value: str | None) -> str | None:
        if not value:
            return None
        if key == "area":
            return self.deps.catalogue.area(value)
        if key == "time_earliest":
            window = RuleModel._times(f"after {value.lower()}")
            return window.get("earliest") if window else None
        if key == "budget_max":
            digits = "".join(ch for ch in value if ch.isdigit())
            return digits or None
        return None

    def _on_status(self, t: Turn, i: Intent) -> None:
        now = t.clock.now()
        bookings = t.tools.list_bookings(user_id=t.user)
        upcoming = [b for b in bookings if b["status"] == "booked" and datetime.fromisoformat(b["slot"]) > now]
        if not upcoming:
            t.say("You have no upcoming bookings.")
            return
        lines = ["Your upcoming bookings:"] + [
            f"- {b['provider_name']} ({t.label(b['service'])}), {t.slot(b['slot'])}, {b['id']}" for b in upcoming]
        t.say("\n".join(lines))

