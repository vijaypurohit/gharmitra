# GharMitra — Design (working draft for review)

> Status: draft for review. The final ≤2-page design note (`docs/DESIGN_NOTE.md`) will be condensed
> from this once the choices below are agreed and implemented.

GharMitra is a chat agent that books, reschedules and cancels home-service errands against a mocked
provider API. The guiding principle:

**The language model understands; the code decides.** The model only turns a user message into a
structured `Intent`. A deterministic controller owns the state, chooses every tool call, and is the
only component that can perform a side effect — and only through an approval gate.

---

## 1. Key choices (summary)

| # | Decision | Chosen | Alternatives considered | Why |
|---|----------|--------|-------------------------|-----|
| D1 | Language / deps | Python 3.11+, stdlib (`sqlite3`, `dataclasses`), `pytest` | TypeScript; Pydantic; LangGraph | Fastest to build, zero runtime deps, 3-command setup |
| D2 | Agent architecture | Deterministic state machine; model = intent parser | Free LLM tool-calling loop with guardrails | Must-haves must be *enforced in code*; evals must run offline |
| D3 | Model in evals | `ScriptedModel` (canned intent per user turn) | Recorded live-LLM transcripts | Fully reproducible, no keys, no flakiness |
| D4 | Model in CLI/web | `GHARMITRA_MODEL_MODE=offline\|live\|routed`; offline = `RuleModel`; live = any configured provider | Live LLM only | Demo works with or without keys; provider is a config change (§12) |
| D5 | Hold vs approval order | **Hold first, then ask approval on the quoted price** | Approve on search price, then hold | User approves the real price; a hold is reversible and needs no approval |
| D6 | Approval identity | SHA-256 fingerprint of (action, provider, slot, price, booking_id) | Boolean "confirmed" flag | Any change to provider/slot/price mechanically voids it |
| D7 | Idempotency key | Derived from approval id: `{approval_id}:{action}`; written to DB before the call | Random UUID per attempt | Same approval ⇒ same key across retries, repeated "yes" and restarts |
| D8 | Success claims | Always verify via `get_booking_status` before saying "booked" | Trust `book()` response | Spec: never claim success until verified; status is authoritative |
| D9 | Persistence | `GHARMITRA_DATABASE_URL`: SQLite by default, Postgres optional (same schema, thin adapter) | JSON files; ORM | Survives restart; transactional write-ahead log; creds only from env |
| D10 | Mock API | In-process class, own SQLite file, seeded RNG + per-scenario forced outcomes | Local HTTP service | Simpler, deterministic; behaves as an "external" system that outlives agent restarts |
| D11 | Untrusted text | Never reaches the parser; sanitised + flagged; display only | LLM summarises reviews | Structural guarantee, not a prompt-based one |
| D12 | Memory forget | Value erased + tombstone that also blocks re-inference | Hard delete row | "Forgotten facts are never reused" — including via inference from history |
| D13 | Traces | JSONL, one file per run, redacted | OpenTelemetry | Zero deps, easy to read side-by-side in demo |
| D14 | Interface | CLI + small web UI (FastAPI + one static HTML page: chat left, live trace right) | Streamlit; React app | No build step; side-by-side chat/trace is exactly what the demo needs |
| D15 | Nice-to-have | **Proactive follow-up** (submitted); model routing as stretch (§14) | Routing only | Brief says "pick at most one"; follow-up reuses the search/approval/execution path |

---

## 2. Architecture

```
user msg ──► Model.parse() ──► Intent ──► Controller ──► next step
                (LLM / scripted)   (typed)     │
                                               ├─ asks for missing fields
                                               ├─ read-only tools (search, get_provider, list_bookings, hold)
                                               ├─ creates pending Approval, shows exact action
                                               └─ Executor (book / cancel / reschedule)
                                                    └─ ApprovalGate.check() ── refuses unless fingerprint matches
Tool output ──► sanitize ──► state / display only (never back into Model.parse)
Everything ──► Tracer (JSONL)         Budget wraps every model + tool call
```

CLI (`gharmitra chat`) and web UI (`gharmitra web`) are thin shells over the same `Controller`.

Modules: `agent/state.py`, `agent/controller.py`, `agent/approval.py`, `agent/executor.py`,
`agent/budget.py`, `agent/sanitize.py`, `llm/*`, `memory/store.py`, `mock_api/*`, `trace.py`, `clock.py`.

`Intent` (what the model can produce — nothing else):
`set_fields{service, area, date_range, time_window, budget_max, provider_pref}`, `choose_option(n)`,
`confirm`, `decline`, `start(action=book|reschedule|cancel, ref)`, `memory_view`, `memory_correct(key,value)`,
`memory_forget(key)`, `smalltalk`. Unknown/invalid model output ⇒ treated as "didn't understand", never as an action.

---

## 3. State model (must-have 1)

```
Session { session_id, user_id, phase, task, candidates[], selected, hold, approval_id, op_key, budget }
Task    { action, service, area, date_range, time_window, budget_max, provider_pref,
          target_booking_id, new_slot }    # every field carries {value, source: user|memory|default}
```

Phases and legal transitions (enforced by a table; illegal transition = bug, raises):

```
COLLECTING ─► SEARCHING ─► PROPOSING ─► HOLDING ─► AWAITING_APPROVAL ─► EXECUTING ─► VERIFYING ─► DONE
     ▲            │             │           │              │                  │            │
     └────────────┴─────────────┴───────────┴──────────────┘ (correction)    └──► FAILED / ABORTED
```

- **Ask only for what's missing.** Required for booking: `service`, `area`, `date_range`. Optional
  (`time_window`, `budget_max`) are asked **once, together**; "no preference" is recorded as a known
  value, so it is never re-asked. Known facts (from the conversation or *stated* memory) are never asked.
- **Corrections keep everything else.** A correction patches one field. Downstream artefacts are
  invalidated by a dependency rule: changing search inputs drops candidates → hold → approval;
  changing only the chosen slot drops hold → approval. Other task fields are untouched.
- Session state is persisted after every turn, so a restart resumes in the same phase.

## 4. Approval enforced by code (must-have 2)

```
Approval { approval_id, session_id, action, provider_id, slot, price, booking_id,
           fingerprint, status: pending|granted|voided|consumed, created_at }
```

1. Controller creates a **pending** approval and shows the exact text:
   *"Book Ravi Plumbing, Thu 6 Nov 09:00–10:00, ₹1,200 (quoted). Confirm? (yes/no)"*
2. Only a `confirm` intent **from a user turn**, while that approval is the one displayed, flips it to
   `granted`. Tool output never goes through the parser, so it cannot produce `confirm`.
3. `Executor` calls `ApprovalGate.check(action, provider, slot, price, booking_id)` immediately before
   every `book` / `cancel` / `reschedule`. It recomputes the fingerprint from the values **about to be
   sent** and compares with the granted record. Mismatch / missing / not granted ⇒ `ApprovalError`, no call.
4. Any change of provider, slot or price (user correction, re-hold with new quote, slot taken) sets the
   approval to `voided` and a fresh one must be shown.
5. After success it becomes `consumed`; a repeated "yes" finds no pending approval and replies with
   the existing booking id.

Reschedule has no hold step in the contract, so its approval covers (booking_id, provider, new_slot,
current price). Cancel's covers (booking_id, provider, slot, price).

## 5. Idempotency and recovery (must-have 3)

Operations log (write-ahead, in SQLite):
`Operation { op_key, approval_id, action, status: started|unknown|succeeded|failed, booking_id, attempts }`

**Book flow**
1. Gate check → insert `Operation(status=started)` → commit → call `book(hold_id, op_key)`.
2. `ok` → `get_booking_status(op_key)` → `booked` ⇒ `succeeded`, tell user.
3. `timeout` → status `unknown` → `get_booking_status(op_key)`:
   `booked` ⇒ succeeded (timeout-after-success); `not_found` ⇒ retry `book` with the **same key**;
   `failed` ⇒ report failure.
4. `slot_taken` ⇒ void approval, offer alternatives. `hold_expired` ⇒ re-hold: same slot and same
   price ⇒ approval still valid, continue; different ⇒ void and re-ask.
5. Max 3 attempts (backoff on fake clock) ⇒ `FAILED` with a clear message.

**Cancel**: transient `503` ⇒ retry with the same key (max 3). **Reschedule**: `slot_conflict` ⇒ void, offer other slots.

**Restart**: on startup, any `Operation` in `started|unknown` is reconciled via `get_booking_status`
**before** handling new input. A repeated "yes" after a restart hits a `consumed`/`succeeded` record.

The mock also de-duplicates on `idempotency_key` (as a real API would), but correctness does not rely
on it: tests assert the agent itself never issues a second `book` without a status check.

## 6. Untrusted tool output (must-have 4)

- Reviews and other free text are **never** passed to `Model.parse` and never become instructions.
- `sanitize.py`: strip control/markup characters, truncate, wrap as quoted data, and flag known
  injection patterns (`ignore previous`, `system:`, `approve`, `book now`, …) → trace `injection_flagged`;
  flagged reviews are shown as "[review hidden: suspicious content]".
- The structural guarantee (approval only from user turns, tool calls chosen only by the controller)
  holds even if the detector misses something. Detection is defence in depth.
- If a live LLM is enabled, it still only sees the user's message plus a compact state summary — never review text.

## 7. Bounded execution (must-have 5)

`Budget` (all configurable): max 8 model calls/turn, max 15 tool calls/turn, max 30 s wall-clock/turn
(injectable clock), max 40 turns/session, max 3 retries per operation. Every model/tool call goes
through `budget.spend()`, which raises `LimitExceeded`.

A single top-level handler turns every exit into a user message plus a trace `exit` event:
`completed | user_cancelled | limit_turns | limit_tool_calls | limit_time | retries_exhausted | unknown_error`.
If a limit is hit during booking, the message says exactly what is known (for example "booking state
unconfirmed; I'll check again on your next message", backed by the operations log).

## 8. Memory (must-have 6)

```
Fact { user_id, key, value, source: stated|inferred, confirmed, status: active|forgotten, updated_at }
keys: area, earliest_time, latest_time, preferred_provider:<service>, budget_max:<service>
```

- **Stated**: the user said it ("I'm in Koramangala", "not before 10 am"). Saved and reused, and shown
  when reused ("Using your saved area: Koramangala").
- **Inferred**: derived by the agent (for example the same electrician used twice in `list_bookings`).
  It is never used silently: the agent asks "Last time you used Sharma Electricals, book them again?"
  A yes turns it into stated.
- **View / correct / forget**: "what do you remember", "my area is HSR, not Koramangala",
  "forget my area". Forget erases the value and leaves a tombstone. The tombstone blocks re-inference
  of that key until the user states it again, and purges it from the live task if it came from memory.

## 9. Traces (must-have 7)

`traces/<run_id>.jsonl`: one run is one CLI session or one eval scenario. Events: `run_start`,
`user_msg`, `intent`, `state_transition{from,to,reason}`, `model_call{model,latency_ms,tokens_in,tokens_out,cost}`,
`tool_call{name,args,result(sanitised),error,latency_ms}`, `approval{created|granted|voided|consumed}`,
`operation{...}`, `memory{write|forget}`, `budget`, `exit{reason}`.
Redaction: keys like `*key*|*token*|*secret*|phone|address` are masked, review text is truncated,
and no environment values are logged. `gharmitra trace <file>` pretty-prints a trace for the demo.

## 10. Evals (must-have 8)

Each scenario has a seed, a mock config (forced outcomes), scripted user turns with their intents,
and assertions on the **final world state** (the mock's bookings) plus **global invariants** that are
checked after every scenario:

- I1: every `book/cancel/reschedule` call matches a granted approval with an identical fingerprint
- I2: at most one active booking per op_key and per (user, provider, slot)
- I3: no "booked" message without a prior `get_booking_status == booked`
- I4: budgets never exceeded; every run has exactly one `exit` event
- I5: forgotten facts are never read; inferred facts are never used unconfirmed

A pytest plugin collects results and `scripts/gen_eval_table.py` writes `docs/eval_table.md`
(scenario · expected · actual · pass/fail · notes).

| # | Scenario | Required |
|---|----------|:-:|
| 1 | Happy path: plumber, this week, mornings, under ₹1,500 | |
| 2 | Missing info: asks only missing fields, never re-asks | |
| 3 | Correction ("make it Saturday") keeps other fields | |
| 4 | Prompt injection in a review | ✅ |
| 5 | Price changes after approval (re-hold quotes higher) | ✅ |
| 6 | Slot changes after approval | ✅ |
| 7 | Timeout after success: status check, no duplicate | ✅ |
| 8 | Timeout, not booked: retry with the same key | |
| 9 | `slot_taken`: alternatives + new approval | |
| 10 | Reschedule `slot_conflict` | ✅ |
| 11 | Repeated confirmation ("yes", "yes", "yes") | ✅ |
| 12 | Memory: correct + forget, never reused | ✅ |
| 13 | Inferred preference needs confirmation | |
| 14 | Restart mid-booking (crash after write-ahead) | ✅ |
| 15 | Limit exhaustion (tool calls / retries) | ✅ |
| 16 | Wall-clock limit | |
| 17 | Cancel with transient 503 | |
| 18 | Hold expired, re-hold at same price continues | |
| 19 | "Same electrician as last time, not before 10 a.m." | |
| 20 | Unknown error gives a clean message | |
| 21 | Provider cancels: replacement found, approval required, no auto-rebook | |
| 22 | Provider cancels: user declines replacement, nothing booked | |
| 23 | Live model returns invalid JSON: falls back to rules, no action taken | |
| 24 | Stale approval from web UI (old `approval_id`) is rejected | |

## 11. Assumptions (the ambiguous parts, written down)

- City: Bengaluru; prices are integer INR; the clock is fixed in tests (Mon 2 Nov 2026, 09:00 IST).
- "This week" means today through Sunday. "Mornings" means 06:00–12:00. Budget comparisons are
  inclusive and use the **quoted** price.
- Holding a slot is not a commitment and needs no approval. Booking, cancelling and rescheduling do.
- One active task per session. A single user per CLI session (`--user`).
- A timeout that "actually booked" is decided by the mock's seeded RNG, or forced per scenario.
- Reschedule keeps the original price. Cancel needs no refund logic.


## 12. Configuration, models and secrets

All configuration comes from environment variables (or a git-ignored `.env`). Defaults run fully offline.

| Variable | Default | Purpose |
|---|---|---|
| `GHARMITRA_MODEL_MODE` | `offline` | `offline` (rules) · `live` (one model) · `routed` (cheap + strong) |
| `GHARMITRA_LLM_PROVIDER` | — | `openai_compat` (OpenAI, Azure, Groq, OpenRouter, Together, Ollama, vLLM) or `gemini` |
| `GHARMITRA_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | — | The strong / single model |
| `GHARMITRA_LLM_CHEAP_PROVIDER` / `_BASE_URL` / `_API_KEY` / `_MODEL` | — | Cheap model, used only in `routed` mode |
| `GHARMITRA_LLM_TIMEOUT_S` | `10` | Per-call timeout |
| `GHARMITRA_DATABASE_URL` | `sqlite:///data/gharmitra.db` | Agent DB; `postgresql://user:pass@host:5432/db` also supported |
| `GHARMITRA_MOCK_DB_PATH` | `data/mock_provider.db` | Mock provider's own store (it's an "external" system) |
| `GHARMITRA_SEED` / `GHARMITRA_SCENARIO` | `42` / `default` | Mock randomness and forced-failure scenario |
| `GHARMITRA_TRACE_DIR` | `traces/` | Trace output |
| `GHARMITRA_MAX_*` | see §7 | Budget caps |

- **Provider adapters** share one interface: `parse(message, state_summary) -> Intent` with JSON-schema
  output. Two adapters cover most providers: OpenAI-compatible chat completions and Gemini. Adding
  another is one file. Token usage and cost (from a per-model price table in config) are reported to the tracer.
- **Failure handling**: timeouts, HTTP errors, or output that fails schema validation fall back to
  `RuleModel` for that turn. The fallback is traced and counts against the budget, and the result can
  never act on its own: a confirm still has to bind to a displayed pending approval.
- **Secrets**: read once at startup and never logged. The tracer masks API keys, `Authorization`
  headers and the password inside `DATABASE_URL`. A test asserts that no configured secret value
  appears in any trace file. `.env` is git-ignored; `.env.example` lists the variables with empty values.
- **Database adapter**: one small `Database` class (connection, transaction, placeholder translation)
  over `sqlite3` and optional `psycopg` (`uv sync --extra postgres`). The schema is portable SQL with
  a unique constraint on `op_key`. Tests and evals run on SQLite.

## 13. Web UI

`gharmitra web` starts FastAPI on `localhost:8000` and serves one static HTML page with no build step.
- Left: the chat. Approval requests appear as a card showing the exact action, provider, slot and
  price, with **Confirm** / **Decline** buttons.
- Right: the run's trace as it happens (state transitions, tool calls, approvals, flagged injections),
  colour-coded, polling `GET /api/trace/{run_id}?after=n`.
- Top: model mode, a scenario picker for the demo (for example *timeout-after-success*,
  *injected review*, *provider cancels*), and a "Memory" drawer to view, correct and forget facts.
- `POST /api/chat {session_id, text, approval_id?}`. A button confirm carries the `approval_id` it was
  rendered for. If that's not the current pending approval, it's rejected (stale-tab protection).
  Typed "yes" binds to the most recently displayed approval, as in the CLI.

## 14. Nice-to-haves

**Proactive follow-up (the one we submit).** The mock gets a scenario/admin hook,
`provider_cancels(booking_id)`, outside the fixed contract, that sets a booking to
`cancelled_by_provider`. On the user's next message, at session start, or when "Check" is clicked in the
web UI, the agent runs `list_bookings` and finds the cancellation. It then tells the user, rebuilds the
task from the original booking (service, area, window, budget, same provider first) and searches for
slots near the original. It holds the best option and shows a **new approval**. It never rebooks
without that approval. Evals 21–22.

**Model routing (stretch, only if time allows).** A per-turn router: short or structured turns
(yes/no, option number, memory commands, single-field answers while `COLLECTING`) go to the cheap
model, or to rules. Free-text requests with several constraints, or corrections, go to the strong model.
If the cheap model's output fails validation, the turn escalates once. Measurement:
`scripts/routing_report.py` runs a labelled set of ~40 utterances through rules / cheap / strong /
routed and writes intent accuracy, p50/p95 latency and cost to `docs/routing_report.md`. Live keys are
needed to regenerate it; the committed report is the evidence. Evals never call a live model.

> ⚠ The brief says *"pick at most one, no extra credit for doing more."* We present proactive follow-up
> as our nice-to-have. Routing would be listed as "built beyond scope" and only after everything else is done.

## 15. Scale (draft for the final note)

What breaks first at 100×: the single-writer SQLite and in-process sessions, hold contention on popular
slots, and per-session trace files. What we'd change: Postgres (already supported) with a unique
constraint on `op_key`, an outbox table with a worker that executes and reconciles operations,
per-(provider, slot) locks, OpenTelemetry traces, per-user rate limits, and model routing to control cost.

## 16. Open questions for review

1. Providers: is OpenAI-compatible + Gemini enough? (OpenAI-compatible also covers Groq, OpenRouter and local Ollama.)
2. Postgres support: keep it (it adds about an hour), or make it SQLite-only and mention Postgres as the 100× change?
3. Model routing: build it as a stretch goal, or drop it to protect the deadline?
4. Are any of the assumptions in §11 wrong for your reading of the brief?
