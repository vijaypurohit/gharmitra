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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gharmitra.clock import SystemClock
from gharmitra.config import Settings, load_settings
from gharmitra.db import drop_schema
from gharmitra.memory.store import label
from gharmitra.mock_api import seed_data as sd
from gharmitra.mock_api.scenarios import SCENARIOS, get_scenario
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
        with self.lock:
            w = self.world
            for db in (w.agent_db, w.mock_db):
                drop_schema(db)
            self.world = build(self.settings, url=w.agent_db.url, agent_schema=w.agent_db.schema,
                               mock_schema=w.mock_db.schema, clock=w.clock,
                               scenario=get_scenario(scenario, self.settings.seed))
            self.session_locks.clear()


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
        return {"model": settings.model_adapter, "scenario": w.scenario.name, "city": w.catalogue.city,
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
            "select id, provider_name, service, slot_start, price, status from bookings where user_id = %s"
            " order by slot_start desc", (user_id,))
        return [{**r, "slot_start": r["slot_start"].isoformat(), "price": format_inr(r["price"])} for r in rows]

    @api.get("/api/memory")
    def memory(user_id: str = sd.DEMO_USER) -> list[dict]:
        rows = state.world.agent_db.all(
            "select key, value, status from facts where user_id = %s and status <> 'forgotten' order by key", (user_id,))
        return [{**r, "label": label(r["key"])} for r in rows]

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api
