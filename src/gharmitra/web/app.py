"""FastAPI app: chat, live trace (SSE), bookings, memory and settings.

Plain HTML, CSS and ES modules from web/static; no build step.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gharmitra import receipts, report
from gharmitra.agent.cancellation import Policy, PolicyStore
from gharmitra.clock import SystemClock
from gharmitra.config import EDITABLE, Settings, SettingsStore, editable_values, load_settings
from gharmitra.db import drop_schema, jsonb, migrate
from gharmitra.llm.factory import probe
from gharmitra.memory.store import MemoryStore, label
from gharmitra.mock_api import seed_data as sd
from gharmitra.mock_api.scenarios import SCENARIOS, Scenario, get_scenario
from gharmitra.money import format_inr
from gharmitra.wiring import World, build

STATIC = Path(str(resources.files("gharmitra.web").joinpath("static")))
SCENARIO_NOTES = {
    "default": "Seeded 70/15/15 book outcomes, occasional quote drift",
    "happy": "Every booking succeeds",
    "timeout_after_success": "First book times out but actually booked",
    "injected_review": "Injection in reviews, a provider name and attributes",
    "slot_taken": "First book returns slot_taken",
    "provider_cancels": "Bookings succeed; use \"Provider cancels\" to trigger a follow-up",
}


class ChatIn(BaseModel):
    session_id: str
    text: str
    approval_id: str | None = None


class SessionIn(BaseModel):
    user_id: str = sd.DEMO_USER


class ProviderCancelIn(BaseModel):
    booking_id: str


class SettingsIn(BaseModel):
    changes: dict[str, Any]


class PolicyIn(BaseModel):
    platform: dict[str, Any] | None = None
    overrides: dict[str, dict[str, Any] | None] = {}


class FactIn(BaseModel):
    value: str


class TestModelIn(BaseModel):
    text: str = "plumber in HSR tomorrow morning"


class ResetIn(BaseModel):
    scenario: str = "default"


class App:
    """Holds the current world; a reset rebuilds it with another scenario."""

    def __init__(self, settings: Settings, world: World | None = None) -> None:
        self.settings = settings
        self.world = world or build(settings, clock=SystemClock())
        self.lock = threading.Lock()
        self.session_locks: dict[str, threading.Lock] = {}

    def session_lock(self, session_id: str) -> threading.Lock:
        with self.lock:
            return self.session_locks.setdefault(session_id, threading.Lock())

    def reset(self, scenario: str) -> None:
        """Wipe bookings, memory and chats and reseed with `scenario`. Saved settings and policy stay."""
        with self.lock:
            w = self.world
            kept = w.agent_db.all("select key, value, updated_at from settings")
            for db in (w.agent_db, w.mock_db):
                drop_schema(db)
            migrate(w.agent_db, "agent")
            for row in kept:
                w.agent_db.execute("insert into settings (key, value, updated_at) values (%s, %s, %s)",
                                   (row["key"], jsonb(row["value"]), row["updated_at"]))
            self._build(get_scenario(scenario, self.settings.seed))
            self.session_locks.clear()

    def rebuild(self) -> None:
        """Apply changed settings: same database and scenario, new model, limits and controller."""
        with self.lock:
            self._build(self.world.scenario)

    def _build(self, scenario: Scenario) -> None:
        w = self.world
        self.world = build(self.settings, dbs=(w.agent_db, w.mock_db), clock=w.clock, scenario=scenario)


def create_app(settings: Settings | None = None, world: World | None = None) -> FastAPI:
    settings = settings or load_settings()
    state = App(settings, world)
    api = FastAPI(title="GharMitra", docs_url="/api/docs")
    api.state.gm = state

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @api.get("/api/info")
    def info() -> dict:
        w = state.world
        return {"model": w.controller.deps.model.adapter, "scenario": w.scenario.name, "city": w.catalogue.city,
                "users": [{"id": sd.DEMO_USER, "name": f"{sd.DEMO_USER_NAME} (has history)"},
                          {"id": sd.NEW_USER, "name": "New user"}],
                "scenarios": [{"name": n, "note": SCENARIO_NOTES.get(n, "")} for n in SCENARIOS]}

    @api.post("/api/reset")
    def reset(body: ResetIn) -> dict:
        if body.scenario not in SCENARIOS:
            raise HTTPException(400, f"unknown scenario {body.scenario}")
        state.reset(body.scenario)
        return {"scenario": body.scenario}

    @api.post("/api/sessions")
    def new_session(body: SessionIn) -> dict:
        return {"session_id": state.world.controller.start_session(body.user_id)}

    @api.get("/api/sessions/{session_id}/messages")
    def messages(session_id: str) -> list[dict]:
        c = state.world.controller
        if c.sessions.get(session_id) is None:
            raise HTTPException(404, "unknown session")
        return [{"role": m["role"], "text": m["text"], "at": m["created_at"].isoformat()}
                for m in c.sessions.messages(session_id)]

    @api.post("/api/chat")
    def chat(body: ChatIn) -> dict:
        c = state.world.controller
        if c.sessions.get(body.session_id) is None:
            raise HTTPException(404, "unknown session")
        with state.session_lock(body.session_id):
            reply = c.handle(body.session_id, body.text, approval_id=body.approval_id)
        return {"messages": reply.messages, "phase": reply.phase, "approval": reply.approval,
                "exit": reply.exit}

    @api.get("/api/sessions/{session_id}/events")
    def events(session_id: str, after: int = 0) -> StreamingResponse:
        db = state.world.agent_db

        def stream() -> Iterator[str]:
            last = after
            idle = 0
            while idle < 3600:  # an hour without events ends the stream; the browser reconnects
                rows = db.all("select seq, ts, type, payload from trace_events where session_id = %s and seq > %s"
                              " order by seq limit 200", (session_id, last))
                for r in rows:
                    last = r["seq"]
                    event = {"seq": r["seq"], "ts": r["ts"].isoformat(), "type": r["type"], "payload": r["payload"]}
                    yield f"id: {last}\ndata: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"
                idle = 0 if rows else idle + 1
                if not rows:
                    if idle % 30 == 0:
                        yield ": keep-alive\n\n"
                    time.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.post("/api/demo/provider-cancels")
    def provider_cancels(body: ProviderCancelIn) -> dict:
        """Demo hook: the provider cancels a booking; the agent follows up in a new session."""
        with state.lock:
            reply = state.world.provider_cancels(body.booking_id)
        if reply is None:
            raise HTTPException(404, f"no active booking {body.booking_id}")
        return {"session_id": reply.session_id, "messages": reply.messages, "phase": reply.phase,
                "approval": reply.approval, "exit": reply.exit}

    @api.get("/api/bookings")
    def bookings(user_id: str = sd.DEMO_USER) -> list[dict]:
        rows = state.world.agent_db.all(
            "select b.id, b.provider_id, b.provider_name, b.service, b.area, b.slot_start, b.price, b.status,"
            " r.fee, r.amount as refund, r.credit,"
            " (select array_agg(receipt_no order by created_at) from receipts where booking_id = b.id) as receipts"
            " from bookings b left join refunds r on r.booking_id = b.id"
            " where b.user_id = %s order by b.slot_start desc", (user_id,))
        cat = state.world.catalogue
        return [{**r, "slot_start": r["slot_start"].isoformat(), "price": format_inr(r["price"]),
                 "service_label": cat.service_label(r["service"]),
                 "fee": format_inr(r["fee"]) if r["fee"] is not None else None,
                 "refund": format_inr(r["refund"]) if r["refund"] is not None else None,
                 "credit": format_inr(r["credit"]) if r["credit"] else None,
                 "receipts": r["receipts"] or []} for r in rows]

    @api.get("/api/receipts/{receipt_no}", response_model=None)
    def receipt(receipt_no: str, request: Request) -> dict | HTMLResponse | PlainTextResponse:
        r = receipts.get(state.world.agent_db, receipt_no)
        if r is None:
            raise HTTPException(404, "unknown receipt")
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(receipts.as_html(r))
        if "text/plain" in accept:
            return PlainTextResponse(receipts.as_text(r))
        return receipts.as_json(r)

    @api.get("/api/sessions/{session_id}/report")
    def session_report(session_id: str) -> dict:
        if state.world.controller.sessions.get(session_id) is None:
            raise HTTPException(404, "unknown session")
        events = report.load_events(state.world.agent_db, session_id)
        r = report.build_report(events)
        return {**r, "text": report.as_text(r)}

    @api.get("/api/policy")
    def policy() -> dict:
        store = PolicyStore(state.world.agent_db, state.world.clock)
        return {"platform": store.platform().to_dict(), "overrides": store.overrides()}

    # --- memory -------------------------------------------------------------------------------

    def memory_store(user_id: str) -> MemoryStore:
        w = state.world
        return MemoryStore(w.agent_db, w.clock, w.controller.tracer(f"memory-{user_id}"))

    @api.get("/api/memory")
    def memory(user_id: str = sd.DEMO_USER) -> list[dict]:
        w = state.world
        rows = w.agent_db.all(
            "select key, value, status, updated_at from facts where user_id = %s and status <> 'forgotten'"
            " order by key", (user_id,))
        names = {pid: name for rows_ in sd.PROVIDERS.values() for pid, name, *_ in rows_}
        return [{"key": r["key"], "label": label(r["key"]), "status": r["status"], "value": r["value"],
                 "display": names.get(r["value"], r["value"]) if r["key"].startswith("provider:") else r["value"],
                 "updated_at": r["updated_at"].isoformat()} for r in rows]

    @api.put("/api/memory/{key}")
    def correct_fact(key: str, body: FactIn, user_id: str = sd.DEMO_USER) -> dict:
        value = state.world.controller._normalise_fact(key, body.value)
        if value is None:
            raise HTTPException(400, f"can't use {body.value!r} as {label(key)}")
        memory_store(user_id).correct(user_id, key, value)
        return {"key": key, "value": value, "status": "stated"}

    @api.delete("/api/memory/{key}")
    def forget_fact(key: str, user_id: str = sd.DEMO_USER) -> dict:
        return {"key": key, "forgotten": bool(memory_store(user_id).forget(user_id, key))}

    # --- settings and policy ----------------------------------------------------------------------

    def settings_view() -> dict:
        w = state.world
        eff = w.settings
        return {
            "values": editable_values(eff),
            "sources": {k: eff.sources.get(k, "default") for k in EDITABLE},
            "secrets": {"MODEL_API_KEY": "set" if eff.model_api_key else "not set",
                        "DATABASE_URL": "set" if eff.database_url else "bundled Postgres"},
            "locale": {"city": eff.city, "currency": eff.currency, "timezone": eff.timezone},
            "scenario": w.scenario.name, "seed": eff.seed,
            "adapters": ["rules", "openai", "anthropic", "claude_cli"],
        }

    @api.get("/api/settings")
    def get_settings() -> dict:
        return settings_view()

    @api.put("/api/settings")
    def put_settings(body: SettingsIn) -> dict:
        w = state.world
        try:
            SettingsStore(w.agent_db).update(body.changes, w.clock.now())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        state.rebuild()
        return settings_view()

    @api.post("/api/settings/test-model")
    def test_model(body: TestModelIn) -> dict:
        w = state.world
        return probe(w.settings, body.text, w.clock.now().date())

    @api.put("/api/policy")
    def put_policy(body: PolicyIn) -> dict:
        store = PolicyStore(state.world.agent_db, state.world.clock)
        try:
            if body.platform is not None:
                store.set_platform(Policy.from_dict(body.platform, store.platform()))
            for provider_id, changes in body.overrides.items():
                store.set_override(provider_id, changes)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        return policy()

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api
