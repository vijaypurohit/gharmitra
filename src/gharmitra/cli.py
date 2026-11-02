"""Command line: `gharmitra chat`, `gharmitra web`, `gharmitra report`, `gharmitra check-model`."""

from __future__ import annotations

import argparse
import json
import sys
import time

from gharmitra.mock_api import seed_data as sd
from gharmitra.mock_api.scenarios import SCENARIOS


def _chat(args: argparse.Namespace) -> int:
    from gharmitra.config import load_settings
    from gharmitra.mock_api.scenarios import get_scenario
    from gharmitra.wiring import build

    settings = load_settings()
    world = build(settings, scenario=get_scenario(args.scenario or settings.scenario, settings.seed))
    controller = world.controller
    session_id = args.session
    if session_id and controller.sessions.get(session_id) is None:
        print(f"No session {session_id}; starting a new one.", file=sys.stderr)
        session_id = None
    session_id = session_id or controller.start_session(args.user)
    print(f"GharMitra · session {session_id} · user {args.user} · model {settings.model_adapter}"
          f" · scenario {world.scenario.name}")
    print(f"Trace: traces/{session_id}.jsonl (also traces/latest.jsonl). Ctrl-D to quit.")
    print("Demo: /provider-cancels BK-905 simulates a provider cancelling that booking.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\nBye. Resume with: gharmitra chat --session {session_id}")
            return 0
        if not text:
            continue
        if text.startswith("/provider-cancels"):
            booking_id = text.split(maxsplit=1)[1].strip() if " " in text else ""
            followup = world.provider_cancels(booking_id)
            if followup is None:
                print(f"     No active booking {booking_id or '(give an id)'} to cancel.\n")
                continue
            session_id = followup.session_id  # the follow-up opens its own session
            controller = world.controller
            print(f"     [provider cancelled {booking_id}; follow-up session {session_id}]")
            reply = followup
        else:
            reply = controller.handle(session_id, text)
        for n, line in enumerate(reply.text.splitlines()):
            print(("gm > " if n == 0 else "     ") + line)
        print()


def _report(args: argparse.Namespace) -> int:
    from gharmitra.config import load_settings
    from gharmitra.db import Database, resolve_url
    from gharmitra.report import as_text, build_report, load_events

    settings = load_settings()
    events = load_events(Database(resolve_url(settings.database_url, settings.data_dir), "agent"), args.session)
    if not events:
        print(f"No trace for session {args.session}.", file=sys.stderr)
        return 1
    r = build_report(events)
    print(json.dumps(r, indent=2, default=str) if args.json else as_text(r))
    return 0


def _web(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("gharmitra.web.app:create_app", factory=True, host=args.host, port=args.port, log_level="info")
    return 0


def _check_model(args: argparse.Namespace) -> int:
    from datetime import date

    from gharmitra.catalogue import load_catalogue
    from gharmitra.config import load_settings
    from gharmitra.llm.base import Intent
    from gharmitra.llm.factory import make_model
    from gharmitra.llm.prompt import build_prompt

    settings = load_settings()
    model = make_model(settings)
    prompt = build_prompt(args.text, today=date.today(), catalogue=load_catalogue(settings.city),
                          phase="COLLECTING", known_fields=[], awaiting=None)
    started = time.perf_counter()
    result = model.complete(prompt)
    latency = (time.perf_counter() - started) * 1000
    intent = Intent.model_validate_json(result.text)
    print(json.dumps({
        "adapter": model.adapter, "model": model.model, "latency_ms": round(latency, 1),
        "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
        "cost_usd": result.usage.cost_usd,
        "intent": intent.model_dump(mode="json", by_alias=True, exclude_defaults=True),
    }, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gharmitra", description="Home-service errand agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="chat in the terminal")
    chat.add_argument("--user", default=sd.DEMO_USER, help=f"user id (default {sd.DEMO_USER}; {sd.NEW_USER} has no history)")
    chat.add_argument("--scenario", choices=sorted(SCENARIOS), help="mock world scenario (default: SCENARIO env)")
    chat.add_argument("--session", help="resume a session id")
    chat.set_defaults(func=_chat)

    web = sub.add_parser("web", help="web chat with a live trace")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.set_defaults(func=_web)

    rep = sub.add_parser("report", help="totals for one session, from its trace")
    rep.add_argument("session")
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(func=_report)

    check = sub.add_parser("check-model", help="parse one message with the configured model")
    check.add_argument("text")
    check.set_defaults(func=_check_model)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))
