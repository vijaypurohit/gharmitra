"""Eval harness: a seeded world, a scripted user, and the global invariants I1–I7 (test-plan.md).

Assertions are on the final world state (mock bookings, operations, approvals, facts, trace), never on
reply wording.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gharmitra.clock import FakeClock
from gharmitra.config import Limits, Settings
from gharmitra.llm.base import ModelError
from gharmitra.llm.scripted import ScriptedModel
from gharmitra.mock_api import seed_data as sd
from gharmitra.mock_api.scenarios import get_scenario
from gharmitra.trace import MemorySink, PostgresSink
from gharmitra.wiring import World, build

FAKE_SECRET = "sk-test-secret-0123456789abcdef"
WRITE_TOOLS = {"book", "cancel", "reschedule"}

# --- intent builders: what the scripted model returns for each scripted message -------------------------

YES = {"act": "confirm", "confidence": 0.95}
NO = {"act": "decline", "confidence": 0.95}
NO_PREF = {"act": "inform", "entities": {"no_preference": True}}
MORNING = {"earliest": "06:00", "latest": "12:00"}


def book(**entities: Any) -> dict:
    return {"act": "request", "goal": "book", "entities": entities, "confidence": 0.9}


def inform(**entities: Any) -> dict:
    return {"act": "inform", "entities": entities, "confidence": 0.9}


def pick(n: int) -> dict:
    return {"act": "select", "option": n, "confidence": 0.9}


def days(a: str, b: str | None = None) -> dict:
    return {"from": a, "to": b or a}


def memory(op: str, key: str | None = None, value: str | None = None) -> dict:
    return {"act": "memory", "memory": {"op": op, "key": key, "value": value}, "confidence": 0.9}


THIS_WEEK = days("2026-11-02", "2026-11-08")
PLUMBER_REQUEST = book(service="plumber", date=THIS_WEEK, time=MORNING, budget_max=1500)


@dataclass
class Step:
    text: str
    intent: Any  # dict, raw string or ModelError
    approval_id: str | None = None
    before: Callable[[EvalWorld], None] | None = None


@dataclass
class EvalWorld:
    world: World
    model: ScriptedModel
    sink: MemorySink
    clock: FakeClock
    replies: list[Any] = field(default_factory=list)
    session_id: str = ""

    @property
    def controller(self):
        return self.world.controller

    @property
    def db(self):
        return self.world.agent_db

    @property
    def mock(self):
        return self.world.mock_db

    def new_session(self, user: str = sd.DEMO_USER, session_id: str | None = None) -> str:
        self.session_id = self.controller.start_session(user, session_id)
        return self.session_id

    def say(self, text: str, intent: Any, approval_id: str | None = None):
        if text not in self.model.script:
            self.model.add(text, intent)
        reply = self.controller.handle(self.session_id, text, approval_id=approval_id)
        self.replies.append(reply)
        return reply

    def run(self, steps: list[Step]) -> None:
        for s in steps:
            if s.before:
                s.before(self)
            self.say(s.text, s.intent, s.approval_id)

    # --- views over the trace and database ----------------------------------------------------

    def events(self, type: str | None = None, session: str | None = None) -> list[dict]:
        return [e for e in self.sink.events if (type is None or e["type"] == type)
                and (session is None or e["session_id"] == session)]

    def tool_calls(self, name: str | None = None) -> list[dict]:
        return [e["payload"] for e in self.events("tool_call") if name is None or e["payload"]["name"] == name]

    def mock_bookings(self, user: str = sd.DEMO_USER, status: str = "booked") -> list[dict]:
        return self.mock.all("select * from bookings where user_id = %s and status = %s and id not like 'BK-9%%'"
                             " order by id", (user, status))

    def approvals(self) -> list[dict]:
        return self.db.all("select * from approvals order by created_at, id")

    def operations(self) -> list[dict]:
        return self.db.all("select * from operations order by created_at")

    def summary(self) -> str:
        """One line for the eval table's `actual` column."""
        booked = self.mock_bookings()
        calls = {n: len(self.tool_calls(n)) for n in ("book", "get_booking_status", "cancel", "reschedule", "hold_slot")}
        apr = self.approvals()
        statuses = ",".join(a["status"] for a in apr) or "none"
        parts = [f"{len(booked)} new booking{'s' if len(booked) != 1 else ''}" +
                 (f" {', '.join(b['id'] for b in booked)}" if booked else "")]
        parts += [f"{v} {k}" for k, v in calls.items() if v]
        parts.append(f"approvals [{statuses}]")
        exits = [e["payload"]["reason"] for e in self.events("exit")]
        if exits:
            parts.append("exit " + ",".join(exits))
        return "; ".join(parts)


def make_world(schemas, scenario: str = "happy", *, limits: Limits | None = None, crash: set[str] | None = None,
               **overrides: Any) -> EvalWorld:
    clock = FakeClock()
    sink = MemorySink()
    model = ScriptedModel()
    settings = Settings(model_api_key=FAKE_SECRET, trace_sinks=())
    world = build(
        settings, url=schemas.agent.url, agent_schema=schemas.agent.schema, mock_schema=schemas.mock.schema,
        clock=clock, scenario=get_scenario(scenario, **overrides), model=model,
        sinks=[sink, PostgresSink(schemas.agent)], limits=limits, crash_points=crash,
    )
    return EvalWorld(world, model, sink, clock)


# --- global invariants ----------------------------------------------------------------------------------


def check_invariants(ev: EvalWorld) -> list[str]:
    problems: list[str] = []
    events = ev.sink.events
    approvals = {a["id"]: a for a in ev.approvals()}
    user_messages = {m["id"] for m in ev.db.all("select id from messages where role = 'user'")}

    # I1: every write call matches a granted approval whose fingerprint equals the values sent,
    # granted by a user turn, and the gate passed right before the call.
    for n, e in enumerate(events):
        if e["type"] != "tool_call" or e["payload"]["name"] not in WRITE_TOOLS:
            continue
        key = e["payload"]["args"]["idempotency_key"]
        apr_id = key.split(":")[0]
        a = approvals.get(apr_id)
        gates = [g for g in events[:n] if g["type"] == "gate" and g["session_id"] == e["session_id"]]
        last_gate = gates[-1]["payload"] if gates else None
        if a is None:
            problems.append(f"I1: {key} has no approval")
        elif a["granted_by_message"] not in user_messages:
            problems.append(f"I1: {apr_id} not granted by a user turn")
        elif last_gate is None or not last_gate["allowed"] or last_gate["approval_id"] != apr_id:
            problems.append(f"I1: {key} called without a passing gate check")
        elif last_gate["fingerprint"] != a["fingerprint"]:
            problems.append(f"I1: {key} fingerprint differs from approval")

    # I2: at most one active booking per op_key and per (user, provider, slot), on both sides.
    dup_key = ev.mock.scalar("select count(*) from (select idempotency_key from bookings where idempotency_key"
                             " is not null group by 1 having count(*) > 1) d")
    dup_slot = ev.mock.scalar("select count(*) from (select 1 from bookings where status = 'booked'"
                              " group by user_id, provider_id, slot_start having count(*) > 1) d")
    dup_agent = ev.db.scalar("select count(*) from (select op_key from bookings where op_key is not null"
                             " group by 1 having count(*) > 1) d")
    if dup_key or dup_slot or dup_agent:
        problems.append(f"I2: duplicates key={dup_key} slot={dup_slot} agent={dup_agent}")

    # I3: no success claim without verification, and verification only after the last write call.
    for n, e in enumerate(events):
        if e["type"] == "operation" and e["payload"]["status"] == "succeeded":
            key = e["payload"]["op_key"]
            action = key.split(":")[1]
            before = events[:n]
            last_write = max((i for i, x in enumerate(before) if x["type"] == "tool_call"
                              and x["payload"]["name"] in WRITE_TOOLS
                              and x["payload"]["args"].get("idempotency_key") == key), default=-1)
            verify = "get_booking_status" if action == "book" else "list_bookings"
            ok = any(x["type"] == "tool_call" and x["payload"]["name"] == verify and x["payload"]["error"] is None
                     for x in before[last_write + 1:])
            if not ok:
                problems.append(f"I3: {key} succeeded without {verify} after the last call")
    succeeded_keys = [e["payload"]["op_key"] for e in events if e["type"] == "operation"
                      and e["payload"]["status"] == "succeeded"]
    claims = [e for e in events if e["type"] == "agent_msg" and "and verified" in e["payload"]["text"]]
    if len(claims) > len(succeeded_keys):
        problems.append(f"I3: {len(claims)} success messages for {len(succeeded_keys)} verified operations")

    succeeded_at = {e["payload"]["op_key"]: n for n, e in enumerate(events)
                    if e["type"] == "operation" and e["payload"]["status"] == "succeeded"}
    for n, e in enumerate(events):
        if e["type"] == "receipt" and succeeded_at.get(e["payload"]["op_key"], n + 1) > n:
            problems.append(f"I3: receipt {e['payload']['receipt_no']} before its operation was verified")
    receipt_keys = [r["op_key"] for r in ev.db.all("select op_key from receipts where op_key not like 'provider_cancel:%%'")]
    if sorted(receipt_keys) != sorted(succeeded_keys):
        problems.append(f"I3: receipts {receipt_keys} differ from verified operations {succeeded_keys}")

    # I4: caps respected; every exit is followed by a message; at most one exit per turn.
    lim = ev.controller.deps.limits
    for b in ev.events("budget"):
        p = b["payload"]
        if p["model_calls"] > lim.model_calls_per_turn or p["tool_calls"] > lim.tool_calls_per_turn:
            problems.append(f"I4: budget exceeded {p}")
    turn_exits = 0
    for n, e in enumerate(events):
        if e["type"] in ("user_msg", "followup"):
            turn_exits = 0
        if e["type"] == "exit":
            turn_exits += 1
            if turn_exits > 1:
                problems.append("I4: two exits in one turn")
            if not any(x["type"] == "agent_msg" and x["session_id"] == e["session_id"] for x in events[n + 1:]):
                problems.append("I4: exit without a following message")

    # I5: memory-sourced fields are stated facts; confirm comes right after a user's confirm intent.
    for s in ev.db.all("select user_id, task from sessions"):
        for k, f in s["task"].get("fields", {}).items():
            if f.get("source") != "memory":
                continue
            key = {"area": "area", "time": "time_earliest", "budget_max": "budget_max"}.get(k)
            if k == "provider":
                key = f"provider:{s['task']['fields'].get('service', {}).get('value')}"
            row = ev.db.one("select status from facts where user_id = %s and key = %s", (s["user_id"], key))
            if row is None or row["status"] != "stated":
                problems.append(f"I5: field {k} from memory but fact {key} is {row and row['status']}")
    for n, e in enumerate(events):
        if e["type"] == "memory" and e["payload"]["op"] == "confirm":
            intents = [x for x in events[:n] if x["type"] == "intent"]
            if not intents or intents[-1]["payload"]["act"] != "confirm":
                problems.append("I5: inferred fact confirmed without a user yes")

    # I6: every call traced with its fields; no secret, phone or address in the trace.
    for p in ev.tool_calls():
        if not {"name", "args", "result", "error", "latency_ms"} <= set(p):
            problems.append(f"I6: tool_call missing fields {p.get('name')}")
    model_events = [e for e in ev.events("model_call") if e["payload"]["adapter"] == "scripted"]
    if len(model_events) != len(ev.model.prompts):
        problems.append(f"I6: {len(ev.model.prompts)} model calls but {len(model_events)} model_call events")
    dump = json.dumps(events, default=str, ensure_ascii=False)
    if FAKE_SECRET in dump:
        problems.append("I6: secret in trace")
    for row in ev.mock.all("select phone, address from providers"):
        if row["phone"] in dump or row["address"] in dump:
            problems.append("I6: provider phone or address in trace")
            break

    # I7: after a book timeout, the next call for that key is get_booking_status.
    calls = [e["payload"] for e in events if e["type"] == "tool_call"]
    for n, c in enumerate(calls):
        if c["name"] == "book" and c["error"] and c["error"]["code"] == "timeout":
            key = c["args"]["idempotency_key"]
            nxt = next((x for x in calls[n + 1:] if x["args"].get("idempotency_key") == key), None)
            if nxt is not None and nxt["name"] != "get_booking_status":
                problems.append(f"I7: {key} retried with {nxt['name']} before a status check")
    return problems


def rate_limited() -> ModelError:
    return ModelError("rate_limited", "429 Too Many Requests")
