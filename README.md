<img src="src/gharmitra/web/static/logo.svg" width="56" alt="logo">

# GharMitra

GharMitra ("home helper") is a chat assistant that books, reschedules and cancels home services
(plumbers, electricians, AC servicing, cleaning) in Bengaluru against a mocked provider API. It never
double-books, never acts without your approval, and ignores instructions hidden in provider reviews.

Everything runs locally: no real providers, no payments, and no LLM key needed.

> Status: the agent, mock API, CLI, web chat with live trace, provider-cancel follow-up and 32 eval
> scenarios are built. Not yet built: cancellation fees and receipts, the Bookings, Memory and
> Settings pages, and the LLM adapters (the offline `rules` parser is used).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) only. No `.env`, database setup or model key.

```bash
uv sync                    # install (includes a bundled Postgres)
uv run pytest              # unit tests + evals, writes docs/eval_table.md
uv run gharmitra web       # chat + live trace at http://localhost:8000
```

Terminal instead: `uv run gharmitra chat`, and `tail -f traces/latest.jsonl` in a second terminal
for the trace. `--scenario timeout_after_success` (or `injected_review`, `slot_taken`, `happy`) picks a
mock world; `--user u-new` starts without booking history. In the web app, **Reset world** does the same.

Provider cancels: type `/provider-cancels BK-905` in the CLI, or use **Provider cancels…** in the web
app. The agent checks the cancellation with the provider, records a full refund plus a ₹100 credit,
and offers replacements in a new session. It never rebooks without your pick and approval.

## Example

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
          "Reasonable price, explained the problem clearly."
     3. HomeCare Plumbers  ★4.1  Mon 2 Nov 10:00  ₹1,400
          "Did the work neatly and cleaned up after."
     Pick a number.
you> 1
gm > Holding the slot. The provider quoted ₹1,200.
     Book Ravi Plumbing, Mon 2 Nov 10:00, ₹1,200 (quoted). Confirm? (yes/no)
you> yes
gm > Booked and verified: Ravi Plumbing, Mon 2 Nov 10:00. Booking id BK-1001.
```

Also: *"Move my AC servicing to Saturday"*, *"Book the same electrician as last time, but not before
10 a.m."*, *"Cancel my cleaning booking"* (shows the refund first), *"What do you remember about
me?"*, *"Forget my area"*.

## How it works

The model only turns a message into a small JSON intent; a state machine decides what to ask and
which tool to call. Details in the [design note](docs/design.md).

- **Approval is a record**: book, cancel and reschedule pass a gate that compares action, provider,
  slot and amount with what you approved. Any change voids it.
- **No double bookings**: the idempotency key comes from the approval and is saved before the call;
  timeouts are resolved through `get_booking_status`, including after a restart.
- **Untrusted text stays data**: reviews never reach the model and can't grant approval.
- **Bounded**: caps on turns, model calls, tool calls and time; every exit ends with a clear message.
- **Memory**: area, earliest time and preferred technician, marked *stated* or *inferred*; you can
  view, correct and forget them.
- **Traces**: a redacted step-by-step log of each session, in Postgres and `traces/*.jsonl`.

## Models

The default `rules` parser runs offline. To use an LLM, copy a block from
[.env.example](.env.example) (Gemini, OpenAI, Anthropic, `claude` CLI, Ollama, LM Studio, OpenRouter,
Groq) into `.env`. Switching provider is config only: `MODEL_ADAPTER`, `MODEL_BASE_URL`,
`MODEL_NAME`, `MODEL_API_KEY`. A failed or invalid model reply falls back to `rules` for that turn.

Check a model: `uv run gharmitra check-model "plumber in HSR tomorrow morning"`, or **Test model** in
Settings.

## Web app

`uv run gharmitra web` serves plain HTML, CSS and ES modules (no build step) at
<http://localhost:8000>.

```
┌────────────────────────────────────────────────────────────────────────┐
│ [logo] GharMitra   Chat · Bookings · Memory · Settings   model ● ☾      │
├───────────────────────────────────────┬────────────────────────────────┤
│ chat                                  │ trace (live)                    │
│  you: plumber in HSR, mornings…       │ 09:00:01 intent request/book    │
│  ┌ Approve booking ───────────────┐   │ 09:00:01 → SEARCHING            │
│  │ Ravi Plumbing · Thu 5 Nov 09:00│   │ 09:00:02 tool search_providers  │
│  │ ₹1,200 quoted                  │   │ 09:00:03 ⚠ injection_flagged    │
│  │ [Confirm]  [Decline]           │   │ 09:00:05 approval created       │
│  └────────────────────────────────┘   │                                 │
│ [ type a message…              ] [↵]  │ filter: all · tools · approvals │
└───────────────────────────────────────┴────────────────────────────────┘
```

| Page | What it does |
|---|---|
| Chat | Conversation with the live trace beside it (a tab on mobile) |
| Bookings | Status, fees, refunds, receipts; cancel / reschedule start a chat flow, so they still need approval |
| Memory | Saved facts with *stated* / *inferred* badges; correct or forget each |
| Settings | Model and Test model, locale, budgets, cancellation policy, demo scenario; API keys shown only as set / not set |

<details>
<summary>HTTP API</summary>

| Method & path | Purpose |
|---|---|
| `POST /api/sessions` | Start a session → `{session_id}` |
| `GET /api/sessions/{id}/messages` | Chat history |
| `POST /api/chat` | `{session_id, text, approval_id?}` → agent messages, approval card, phase |
| `GET /api/sessions/{id}/events` | Live trace (Server-Sent Events) |
| `POST /api/demo/provider-cancels` | `{booking_id}`: the provider cancels; returns the follow-up session and its messages |
| `GET /api/sessions/{id}/report` | Run report |
| `GET /api/bookings` | Bookings with status, fees and refunds |
| `GET /api/receipts/{receipt_no}` | Receipt (JSON, or HTML with `Accept: text/html`) |
| `GET /api/policy` | Current cancellation policy |
| `GET` · `PUT` · `DELETE /api/memory[/{key}]` | View, correct, forget |
| `GET` · `PUT /api/settings` | Read / update settings |
| `POST /api/settings/test-model` | Parse a sample message → intent, latency, cost |

</details>

## Configuration

Nothing is required. Env vars or a `.env` file (see [.env.example](.env.example)) set defaults and
hold secrets; Settings can override non-secret values. Order: code defaults < env < Settings.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | bundled Postgres in `data/pg` | Postgres; migrations run at startup |
| `TEST_DATABASE_URL` | `DATABASE_URL` | Where tests create and drop schemas |
| `MODEL_ADAPTER` | `rules` | `rules` · `openai` · `anthropic` · `claude_cli` · `module:Class` |
| `MODEL_BASE_URL` · `MODEL_NAME` · `MODEL_API_KEY` | — | Endpoint, model id, key (env only, never stored or shown) |
| `MODEL_TIMEOUT_S` · `MODEL_MIN_INTERVAL_S` | `30` · `0` | Call timeout; gap between calls for free tiers |
| `TRACE_SINKS` | `postgres,jsonl` | Where traces go |
| `CITY` · `CURRENCY` · `TIMEZONE` | `bengaluru` · `INR` · `Asia/Kolkata` | Locale |
| `SEED` · `SCENARIO` | `42` · `default` | Mock randomness and forced failures (timeout after success, injected review, provider cancels) |

Budgets, cancellation policy and trace retention are edited in Settings. City data lives in
`src/gharmitra/cities/`; adding a city is one file.

## Tests

- `uv run pytest`: unit tests and 30+ eval scenarios, offline and reproducible with a scripted model
  and seeded mock. Every eval also checks invariants such as "no booking without a matching
  approval". Writes [docs/eval_table.md](docs/eval_table.md).
- `uv run pytest -m live`: optional checks against the configured model. Skipped by default.

See the [test plan](docs/test-plan.md).

## Project layout

```
src/gharmitra/
  agent/       state machine, intent validation, approval gate, executor, cancellation, budget, sanitiser
  llm/         model interface, prompt builder and adapters
  memory/      user preference store
  mock_api/    fixed-contract provider API and seeded failures
  web/         FastAPI app and static UI
  cities/      city catalogue (services, areas, aliases); one JSON file per city
  migrations/  SQL migrations, shipped with the package
  wiring.py    picks the configured implementation of each component
  db.py · trace.py · receipts.py · config.py · catalogue.py · clock.py · money.py · cli.py
tests/         unit tests and eval scenarios (evals/ also writes the eval table)
docs/          design note, plans, eval table, bug log
traces/        JSONL traces; sample_*.jsonl are committed
```

## Deliverables

| Item | Where |
|---|---|
| Design note | [docs/design.md](docs/design.md) |
| Eval table (generated) | [docs/eval_table.md](docs/eval_table.md) |
| Three bugs and their regression tests | [docs/BUGS.md](docs/BUGS.md) |
| Trace with a timeout after success | [traces/sample_timeout_after_success.jsonl](traces/sample_timeout_after_success.jsonl) |
| Demo video | link added after recording |

Also: [requirements](docs/requirements.md), [implementation plan](docs/implementation-plan.md),
[test plan](docs/test-plan.md).
