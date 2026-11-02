from fastapi.testclient import TestClient

from gharmitra.clock import FakeClock
from gharmitra.config import Settings
from gharmitra.mock_api.scenarios import get_scenario
from gharmitra.trace import PostgresSink
from gharmitra.web.app import create_app
from gharmitra.wiring import build


def client(schemas):
    settings = Settings(trace_sinks=("postgres",))
    world = build(settings, url=schemas.agent.url, agent_schema=schemas.agent.schema,
                  mock_schema=schemas.mock.schema, clock=FakeClock(), scenario=get_scenario("happy"),
                  sinks=[PostgresSink(schemas.agent)])
    return TestClient(create_app(settings, world)), world


def chat(c, sid, text, approval_id=None):
    r = c.post("/api/chat", json={"session_id": sid, "text": text, "approval_id": approval_id})
    assert r.status_code == 200, r.text
    return r.json()


def test_chat_round_trip_and_stale_approval(schemas):
    c, world = client(schemas)
    assert c.get("/").status_code == 200 and "GharMitra" in c.get("/").text
    assert c.get("/api/info").json()["model"] == "rules"
    sid = c.post("/api/sessions", json={"user_id": "u-demo"}).json()["session_id"]
    chat(c, sid, "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500")
    chat(c, sid, "Koramangala")
    assert chat(c, sid, "no preference")["phase"] == "PROPOSING"
    first = chat(c, sid, "1")["approval"]
    second = chat(c, sid, "2")["approval"]
    assert first and second and first["approval_id"] != second["approval_id"]

    stale = chat(c, sid, "Confirm", first["approval_id"])
    assert stale["phase"] == "AWAITING_APPROVAL" and "no longer valid" in stale["messages"][0]
    tools = [r["payload"]["name"] for r in world.agent_db.all("select payload from trace_events where type = 'tool_call'")]
    assert "book" not in tools

    done = chat(c, sid, "Confirm", second["approval_id"])
    assert done["phase"] == "DONE" and done["exit"] == "completed"
    history = c.get(f"/api/sessions/{sid}/messages").json()
    assert history[0]["role"] == "user" and history[-1]["role"] == "agent"
    assert any(b["status"] == "booked" and b["id"].startswith("BK-1") for b in c.get("/api/bookings").json())


def test_unknown_session_and_reset(schemas):
    c, _ = client(schemas)
    assert c.post("/api/chat", json={"session_id": "nope", "text": "hi"}).status_code == 404
    assert c.post("/api/reset", json={"scenario": "nope"}).status_code == 400
    assert c.post("/api/reset", json={"scenario": "injected_review"}).json() == {"scenario": "injected_review"}
    assert c.get("/api/info").json()["scenario"] == "injected_review"
