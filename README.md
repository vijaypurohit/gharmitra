<img src="src/gharmitra/web/static/logo.svg" width="56" alt="logo">

# GharMitra

GharMitra ("home helper") is a chat agent that books, reschedules and cancels home-service
appointments (plumber, electrician, AC servicing, home cleaning) in Bengaluru. It works against a
mocked provider API with a fixed contract and deliberately unreliable behaviour: timeouts that
secretly succeed, slots that get taken, quotes that change, a cancel that fails once, and provider
reviews that try to inject instructions.

The point of the project is correctness under those failures. It:

- **never acts without approval**: book, cancel and reschedule need an approval record that matches
  the exact provider, slot and price; any change voids it;
- **never double-books**: across repeated "yes", retries, timeouts and process restarts;
- **never gets talked into anything** by provider text: reviews, names and attributes are data only;
- **stays bounded**: hard caps on messages, model calls, tool calls and time, each ending with a
  clear message;
- **remembers preferences** (area, earliest time, preferred technician), marked *stated* or
  *inferred*, viewable, correctable and forgettable;
- **traces every step** (state changes, model calls, tool calls with results and latency, tokens and
  cost) with secrets redacted.

Everything runs on your machine: the providers are a mock, no money moves, and no external service
is needed. See [docs/requirements.md](docs/requirements.md) for the brief and
[docs/design.md](docs/design.md) for the design.

## Quick start (3 commands)

Needs only [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by uv if missing).

```bash
uv sync
uv run pytest
uv run gharmitra web
```

These install the dependencies, run the unit tests and eval scenarios (writing
`reports/eval_table.md`), and start the web app at <http://localhost:8000>. No `.env`, database or API key is needed for these: if `DATABASE_URL` is unset, a Postgres server
bundled with the Python package starts under `data/pg`, and messages are understood by the built-in
`rules` parser.

### What "no LLM key needed" means

The agent never lets a model decide what to do. A model only turns your message into a small JSON
*intent* (`{"act": "request", "goal": "book", "entities": {"service": "plumber", ...}}`); code does
everything else. That intent can come from:

- **`rules`** (default): a built-in keyword and pattern parser. Offline, instant, free, no key. It
  handles English and common Hinglish ("kal subah HSR mein plumber chahiye, 1500 tak"), corrections,
  numbered picks, yes/no in many forms, memory commands and status questions. It is also the
  fallback whenever an LLM call fails.
- **an LLM** (optional): better at unusual phrasing. Configure one under [Models](#models).

So the whole project, including all tests and the demo, works with no LLM at all.

## Running it

### Web app

```bash
uv run gharmitra web
uv run gharmitra web --host 0.0.0.0 --port 9000
```

Serves <http://127.0.0.1:8000> by default; `--host` and `--port` change where it listens.

| Page | What it does |
|---|---|
| **Chat** | Conversation on the left, live trace on the right (tabs on a phone). Approval cards with Confirm / Decline. Toolbar: scenario picker + **Reset world**, **Provider cancels…**, **Run report**, **New chat**. Example messages under the input. |
| **Bookings** | Every booking with status, price, fee, refund, credit and receipt links. Cancel / Reschedule open a chat flow, so they still need approval. |
| **Memory** | Saved facts with *stated* / *inferred* badges; Correct or Forget each. |
| **Settings** | Model adapter and **Test model**, budgets, trace retention, cancellation policy; where each value comes from (default, env or saved); API keys only as set / not set. |

The header has the user picker (**Asha**, who has booking history, or **New user**) and a
light / dark / auto theme toggle.

### Terminal

```bash
uv run gharmitra chat
uv run gharmitra chat --user u-new
uv run gharmitra chat --scenario timeout_after_success
uv run gharmitra chat --session s-1a2b3c4d5e6f
tail -f traces/latest.jsonl
```

`--user u-new` chats as a user with no history, `--scenario` picks a [demo scenario](#demo-scenarios),
`--session` resumes a conversation, and `tail -f` in a second terminal shows the live trace.

Inside the chat, `/provider-cancels BK-905` simulates the provider cancelling that booking.

| Command | Purpose |
|---|---|
| `gharmitra chat [--user ID] [--scenario NAME] [--session ID]` | Chat in the terminal |
| `gharmitra web [--host H] [--port P]` | Web app |
| `gharmitra report SESSION_ID [--json]` | Totals for a session from its trace: turns, model and tool calls, approvals, receipts, exits |
| `gharmitra check-model "TEXT"` | Parse one message with the configured model: intent, latency, tokens, cost |

### Things to try

```
Find a plumber for my kitchen tap this week, mornings only, under ₹1,500
Move my AC servicing to Saturday
Book the same electrician as last time, but not before 10 a.m.
Cancel my cleaning booking
What do you remember about me? / Forget my area / Remember I live in HSR
make it Saturday instead
yes, yes, yes
```

Example run:

```
you> Find a plumber for my kitchen tap this week, mornings only, under ₹1,500
gm > Which area are you in?
you> Koramangala
gm > Any preferred provider? Say "no preference" to skip.
you> no preference
gm > Here are the best matches:
     1. Ravi Plumbing  ★4.6  Mon 2 Nov 10:00  ₹1,200
          "Good job overall, slightly late but called ahead."
          [review hidden: suspicious content]
     2. QuickFix Pipes  ★4.3  Mon 2 Nov 11:00  ₹950
     3. HomeCare Plumbers  ★4.1  Mon 2 Nov 10:00  ₹1,400
     Pick a number.
you> 1
gm > Holding the slot. The provider quoted ₹1,200.
     Book Ravi Plumbing, Mon 2 Nov 10:00, ₹1,200 (quoted). Confirm? (yes/no)
you> yes
gm > Booked and verified: Ravi Plumbing, Mon 2 Nov 10:00. Booking id BK-1001. Receipt GM-20261102-0001.
```

### Demo scenarios

The mock world is seeded (same providers, slots and reviews every time). A scenario forces specific
failures. Pick one with **Reset world** in the web app, `--scenario` in the CLI, or `SCENARIO=`.

| Scenario | What happens |
|---|---|
| `default` | Seeded mix of `book` outcomes (70% ok · 15% `slot_taken` · 15% timeout, half of which actually booked); occasional quote drift |
| `happy` | Every booking succeeds |
| `timeout_after_success` | The first `book` times out but the provider did book it; the agent finds it via `get_booking_status` |
| `injected_review` | Injection attempts in reviews, a provider name and an attribute |
| `slot_taken` | The first `book` returns `slot_taken`; the agent offers fresh options |
| `provider_cancels` | Bookings succeed; use **Provider cancels…** to see the follow-up (refund + ₹100 credit, replacements, new approval) |

Every scenario has at least one injection review, and `cancel` always fails once with a `503`
before succeeding, as the contract requires.

## Testing

```bash
uv run pytest
uv run pytest tests/evals
uv run pytest -k e7
uv run pytest -m live
```

The first runs everything offline; the next two run only the eval scenarios or one of them; `-m live`
runs the optional checks against the configured model, which are skipped by default.

- **Evals** (`tests/evals/`): 34 scenarios with scripted simulated users. Each asserts the final world
  state (provider bookings, operations, approvals, memory, trace), not reply wording, and then checks
  seven global invariants, e.g. "every book / cancel / reschedule call matches a granted approval
  whose fingerprint equals the values sent" and "after a book timeout, the next call is a status
  check". They cover prompt injection, price and slot changes after approval, timeout after success,
  slot conflicts, repeated confirmation, memory correct and forget, a restart mid-booking, and every
  limit. A full run writes **[reports/eval_table.md](reports/eval_table.md)** (scenario · expected ·
  actual · pass/fail · notes) and the sample trace
  [traces/sample_timeout_after_success.jsonl](traces/sample_timeout_after_success.jsonl).
- **Unit tests** (`tests/unit/`): mock API contract, state machine, approval gate, executor and
  recovery, sanitiser, budgets, memory, tracer redaction, cancellation policy, receipts, settings,
  web API, model adapters (stubbed HTTP and a fake `claude` binary) and ~70 phrasings for the rules
  parser.
- **Live checks** (`tests/live/`, opt-in): intent accuracy on 25 labelled messages and four
  end-to-end flows with the configured model; results go to
  [reports/live_model_report.md](reports/live_model_report.md), one section per model.

Tests use a fake clock (Mon 2 Nov 2026, 09:00 IST), a seeded mock and a fresh pair of Postgres
schemas per test in `TEST_DATABASE_URL` (or `DATABASE_URL`, or the bundled server), dropped
afterwards. They never write to your app data.

## Configuration

Nothing is required. Settings come from, in increasing priority: code defaults → environment
variables or `.env` → values saved on the **Settings** page. Copy [.env.example](.env.example) to
`.env` to start. Secrets (API keys, `DATABASE_URL`) are read from the environment only; they are never
stored, logged, traced or returned by the API.

| Variable | Default | Options and meaning |
|---|---|---|
| `DATABASE_URL` | unset → bundled Postgres in `data/pg` | Any Postgres URL, e.g. `postgresql://user:pass@localhost:5432/gharmitra`. Migrations run at startup. |
| `TEST_DATABASE_URL` | `DATABASE_URL` | Database where tests create and drop throwaway schemas |
| `DATA_DIR` | `data` | Where the bundled Postgres keeps its files |
| `TRACES_DIR` | `traces` | Where JSONL traces are written (`<session>.jsonl` and `latest.jsonl`) |
| `MODEL_ADAPTER` | `rules` | `rules` · `openai` · `anthropic` · `claude_cli` · `your.module:YourClass` (see [Models](#models)) |
| `MODEL_BASE_URL` | per adapter | API endpoint, e.g. Gemini's or a local Ollama |
| `MODEL_NAME` | per adapter | Model id, e.g. `gemini-3.5-flash`, `claude-sonnet-5-5`, `sonnet` |
| `MODEL_API_KEY` | — | Key for the endpoint (env only) |
| `MODEL_TIMEOUT_S` | `30` | Per-call timeout; on timeout that message falls back to `rules` |
| `MODEL_MIN_INTERVAL_S` | `0` | Minimum gap between model calls, for free tiers: 60 ÷ requests-per-minute |
| `TRACE_SINKS` | `postgres,jsonl` | Where traces go: `postgres` (powers the live trace and reports), `jsonl`, or both |
| `TRACE_RETENTION_DAYS` | `30` | Traces of sessions idle longer than this are deleted at startup; `0` keeps all |
| `CITY` | `bengaluru` | City catalogue in `src/gharmitra/cities/<city>.json` (services, areas, aliases) |
| `CURRENCY` · `TIMEZONE` | `INR` · `Asia/Kolkata` | Money and the user's timezone (used only for what users see and for "today" / "mornings") |
| `SEED` | `42` | Seed for the mock world: providers, slots, reviews, outcomes |
| `SCENARIO` | `default` | Demo scenario, see [Demo scenarios](#demo-scenarios) |

Saved on the Settings page (and `PUT /api/settings`): model adapter, endpoint, name, timeout and
gap; trace retention; and the budgets below. Clearing a field goes back to the env or default value.

| Budget | Default | Meaning |
|---|---|---|
| Model calls per message | 8 | Includes fallbacks |
| Tool calls per message | 15 | Provider API calls |
| Seconds per message | 60 | Wall-clock, including retries and backoff |
| Messages per session | 40 | Then the session asks you to start a new one |
| Retries per operation | 3 | Same idempotency key, only while the outcome is unknown |
| Min model confidence | 0.6 | Below it, the agent asks instead of guessing |

**Cancellation policy** (Settings page or `PUT /api/policy`): free 24 h or more before the slot,
₹100 between 3 and 24 h, 50% under 3 h, no cancelling after the start; rescheduling allowed until
3 h before; ₹100 credit when the provider cancels. Per-provider overrides are supported. Each
booking keeps the policy it was made under.

**Times** are handled in UTC internally (database, provider API, traces). Only what the user reads,
and calendar words like "today" or "mornings", use `TIMEZONE`.

### Using your own Postgres

The bundled server is fine for local use. To use a regular server instead (example: Homebrew):

```bash
brew services start postgresql@15
psql -d postgres -c "create role gharmitra login password 'change-me'"
psql -d postgres -c "create database gharmitra owner gharmitra"
psql -d postgres -c "create database gharmitra_test owner gharmitra"
```

then in `.env`:

```bash
DATABASE_URL=postgresql://gharmitra:change-me@localhost:5432/gharmitra
TEST_DATABASE_URL=postgresql://gharmitra:change-me@localhost:5432/gharmitra_test
```

Tables are created on first start. The app uses two schemas in that database: `agent` (sessions,
approvals, operations, bookings, memory, traces, settings) and `mock` (the provider side).

## Models

All adapters return the same intent JSON and are interchangeable by configuration. If a call fails
(timeout, rate limit, server error, invalid JSON, schema mismatch), that message is parsed by `rules`
instead and the fallback is recorded in the trace. A misconfigured adapter (e.g. `openai` with no
`MODEL_NAME`) logs a warning and uses `rules`.

| `MODEL_ADAPTER` | Talks to | Needs | Default model |
|---|---|---|---|
| `rules` | Nothing; built-in parser | — | — |
| `openai` | Any **OpenAI-compatible** `/chat/completions` endpoint | `MODEL_BASE_URL`, `MODEL_NAME`, usually `MODEL_API_KEY` | none |
| `anthropic` | Claude Messages API (official SDK), structured JSON output | `MODEL_API_KEY` or `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` |
| `claude_cli` | Your logged-in `claude` command (Claude Code); no API key or credits | `claude` on `PATH` | `sonnet` |
| `module:Class` | Your own class with `complete(prompt) -> ModelResult` | — | — |

**Why several providers use `openai`:** the name refers to the request format, not the company.
Gemini, Ollama, LM Studio, OpenRouter, Groq and vLLM all accept the OpenAI chat-completions format,
so one adapter covers them; `MODEL_BASE_URL` picks the provider. Ready-made blocks are in
[.env.example](.env.example):

| Provider | `MODEL_BASE_URL` | `MODEL_NAME` example |
|---|---|---|
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash` |
| OpenAI | `https://api.openai.com/v1` | an OpenAI model id |
| Ollama (local) | `http://localhost:11434/v1` | `qwen2.5:7b` |
| LM Studio (local) | `http://localhost:1234/v1` | the loaded model |
| OpenRouter | `https://openrouter.ai/api/v1` | `vendor/model` |
| Groq | `https://api.groq.com/openai/v1` | a Groq model id |

**Choosing a Gemini model:** list what your key can use with
`curl -H "Authorization: Bearer $KEY" https://generativelanguage.googleapis.com/v1beta/openai/models`
(or see Google AI Studio). Flash and Flash-Lite models are fast and on the free tier; Pro models are
slower. Set `MODEL_MIN_INTERVAL_S` to 60 ÷ your requests-per-minute limit. On the free tier,
responses can take 5–20 s and popular models sometimes answer `503` (busy); the agent then falls back
to `rules` for that message.

Check a configuration with `uv run gharmitra check-model "plumber in HSR tomorrow morning"` or
**Test model** in Settings. Measured results per model are in
[reports/live_model_report.md](reports/live_model_report.md).

## How it works

```
message → model or rules → intent (JSON, validated) → state machine → tools → sanitiser → reply
                                                           │
                                       book / cancel / reschedule only through:
                                       approval gate → write-ahead operation → call → verify → receipt
```

- **State machine**: COLLECTING → SEARCHING → PROPOSING → HOLDING → AWAITING_APPROVAL → EXECUTING →
  VERIFYING → DONE (or ABORTED / FAILED). It asks only for missing fields; a correction clears only
  what depends on it. State is saved after every message, so a restart resumes in place.
- **Approval**: a database row with a fingerprint of action, provider, slot (UTC) and amount, granted
  only by a user message that saw it. The gate recomputes the fingerprint from the values about to be
  sent, right before every call. Any change voids the approval.
- **Idempotency and recovery**: one approval → one operation → one idempotency key, saved before the
  call. A timeout is resolved with `get_booking_status`, never by calling `book` again blindly.
  Success is claimed only after verification. Open operations are settled before the next message is
  handled, including after a restart.
- **Untrusted text**: reviews, names and attributes are sanitised, flagged and shown as data; they
  never reach the model's prompt and cannot grant approval.
- **Traces**: each session has one trace (Postgres + `traces/<session>.jsonl`): state transitions,
  intents, model calls (adapter, latency, tokens, cost), tool calls (args, sanitised result, error,
  latency), approvals, gate checks, operations, receipts, memory changes, budgets and the exit reason.
  API keys, tokens, passwords, phone numbers and addresses are redacted.

Details, the data model and "what breaks first at 100×" are in [docs/design.md](docs/design.md).

<details>
<summary>HTTP API</summary>

| Method & path | Purpose |
|---|---|
| `GET /api/info` | Model, scenario, city, users, scenarios |
| `POST /api/sessions` | `{user_id}` → `{session_id}` |
| `GET /api/sessions/{id}/messages` | Chat history |
| `POST /api/chat` | `{session_id, text, approval_id?}` → agent messages, approval card, phase, exit |
| `GET /api/sessions/{id}/events` | Live trace (Server-Sent Events) |
| `GET /api/sessions/{id}/report` | Run report (JSON + text) |
| `POST /api/demo/provider-cancels` | `{booking_id}`: the provider cancels; returns the follow-up session and messages |
| `POST /api/reset` | `{scenario}`: wipe bookings, memory and chats and reseed; settings and policy stay |
| `GET /api/bookings?user_id=` | Bookings with status, fees, refunds, credit and receipt numbers |
| `GET /api/receipts/{receipt_no}` | Receipt as JSON, HTML (`Accept: text/html`) or text (`Accept: text/plain`) |
| `GET` · `PUT /api/policy` | Cancellation policy: `{platform: {...}, overrides: {provider_id: {...} or null}}` |
| `GET /api/memory?user_id=` · `PUT` · `DELETE /api/memory/{key}` | View, correct, forget |
| `GET` · `PUT /api/settings` | Read / update: `{changes: {key: value or null}}` |
| `POST /api/settings/test-model` | `{text}` → parsed intent, latency, tokens, cost |

Interactive docs at <http://localhost:8000/api/docs>.

</details>

## Project layout

```
src/gharmitra/
  agent/       controller (state machine), intents, approval gate, executor, tools wrapper,
               sanitiser, budget, cancellation policy, provider-cancel follow-up
  llm/         intent schema, prompt builder, rules parser, adapters (openai, anthropic, claude_cli)
  memory/      preference store (stated / inferred / forgotten)
  mock_api/    the provider API (fixed contract), seed data and scenarios
  web/         FastAPI app and the static UI (HTML, CSS, JS, fonts; no build step)
  cities/      city catalogues
  migrations/  SQL migrations for the agent and mock schemas
  wiring.py · cli.py · config.py · db.py · clock.py · trace.py · receipts.py · report.py · money.py · catalogue.py
tests/
  unit/        unit tests
  evals/       eval scenarios, invariants and the eval-table writer
  live/        opt-in checks against a real model
docs/          requirements, design note, bug log
reports/       generated: eval table, live model report
traces/        JSONL traces; only sample_*.jsonl is committed
data/          bundled Postgres (created on first run, not committed)
```

## Deliverables

| Item | Where |
|---|---|
| Implementation, mocks, tests | this repo |
| Design note | [docs/design.md](docs/design.md) |
| Eval table (generated by `uv run pytest`) | [reports/eval_table.md](reports/eval_table.md) |
| Bugs hit while building, with regression tests | [docs/BUGS.md](docs/BUGS.md) |
| End-to-end trace with a timeout after success | [traces/sample_timeout_after_success.jsonl](traces/sample_timeout_after_success.jsonl), excerpt in design note §9 |
