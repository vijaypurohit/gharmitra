"""Settings precedence (defaults < env < Settings page) and the Settings, Memory and policy APIs."""

import json
from datetime import datetime

import pytest

from gharmitra.config import Settings, SettingsStore, apply_overrides, load_settings
from tests.unit.test_web import chat, client

SECRET = "sk-live-do-not-store-1234567890"
NOW = datetime(2026, 11, 2, 9, 0)


def test_precedence_defaults_env_settings(schemas):
    env = load_settings(env={"MODEL_NAME": "env-model", "MODEL_TIMEOUT_S": "12"}, dotenv=None)
    assert (env.model_name, env.model_timeout_s, env.limits.tool_calls_per_turn) == ("env-model", 12.0, 15)
    store = SettingsStore(schemas.agent)
    store.update({"model_name": "saved-model", "limits.tool_calls_per_turn": 9}, now=NOW)
    s = apply_overrides(env, store.get())
    assert (s.model_name, s.model_timeout_s, s.limits.tool_calls_per_turn) == ("saved-model", 12.0, 9)
    assert s.sources["model_name"] == "settings" and s.sources["model_timeout_s"] == "env"
    assert s.sources["model_adapter"] == "default"
    store.update({"model_name": None}, now=NOW)  # removing falls back to env
    assert apply_overrides(env, store.get()).model_name == "env-model"


def test_secrets_and_unknown_keys_rejected(schemas):
    store = SettingsStore(schemas.agent)
    for key in ("model_api_key", "database_url", "limits.nope"):
        with pytest.raises(ValueError):
            store.update({key: SECRET}, now=NOW)
    with pytest.raises(ValueError):
        store.update({"limits.tool_calls_per_turn": "many"}, now=NOW)
    assert store.get() == {}


def test_settings_api_never_returns_or_stores_secrets(schemas):
    c, world = client(schemas)
    world.settings = Settings(model_api_key=SECRET, trace_sinks=("postgres",))
    view = c.get("/api/settings").json()
    assert view["secrets"]["MODEL_API_KEY"] == "set" and SECRET not in json.dumps(view)
    assert c.put("/api/settings", json={"changes": {"model_api_key": SECRET}}).status_code == 400
    r = c.put("/api/settings", json={"changes": {"limits.tool_calls_per_turn": 9, "model_name": "m"}})
    assert r.status_code == 200 and r.json()["values"]["limits.tool_calls_per_turn"] == 9
    assert r.json()["sources"]["limits.tool_calls_per_turn"] == "settings"
    app_world = c.app.state.gm.world
    assert app_world.controller.deps.limits.tool_calls_per_turn == 9  # applied without a restart
    assert SECRET not in json.dumps(world.agent_db.all("select value from settings"))


def test_reset_keeps_settings_and_policy(schemas):
    c, _ = client(schemas)
    c.put("/api/settings", json={"changes": {"limits.turns_per_session": 25}})
    c.put("/api/policy", json={"platform": {"late_fee": 20000}})
    c.post("/api/reset", json={"scenario": "happy"})
    assert c.get("/api/settings").json()["values"]["limits.turns_per_session"] == 25
    assert c.get("/api/policy").json()["platform"]["late_fee"] == 20000


def test_policy_api_validates_and_applies_overrides(schemas):
    c, _ = client(schemas)
    assert c.put("/api/policy", json={"platform": {"late_hours": 50}}).status_code == 400
    r = c.put("/api/policy", json={"overrides": {"C-SPARKLE": {"free_hours": 48}}}).json()
    assert r["overrides"] == {"C-SPARKLE": {"free_hours": 48}}
    sid = c.post("/api/sessions", json={"user_id": "u-demo"}).json()["session_id"]
    card = chat(c, sid, "Cancel my cleaning booking")["approval"]
    assert card["amount"] == "₹1,800"  # BK-905 keeps the policy it was booked under (free at 30 h)
    from gharmitra.agent.cancellation import PolicyStore
    world = c.app.state.gm.world
    assert PolicyStore(world.agent_db, world.clock).snapshot("C-SPARKLE")["free_hours"] == 48  # new bookings


def test_test_model_endpoint_with_rules(schemas):
    c, _ = client(schemas)
    r = c.post("/api/settings/test-model", json={"text": "plumber in HSR tomorrow morning"}).json()
    assert r["ok"] and r["adapter"] == "rules" and r["intent"]["entities"]["area"] == "HSR Layout"


def test_memory_api_correct_and_forget(schemas):
    c, _ = client(schemas)
    assert c.put("/api/memory/area", json={"value": "hsr"}).json() == {"key": "area", "value": "HSR Layout",
                                                                       "status": "stated"}
    assert c.put("/api/memory/area", json={"value": "Mars"}).status_code == 400
    facts = c.get("/api/memory").json()
    assert [(f["key"], f["status"], f["display"]) for f in facts] == [("area", "stated", "HSR Layout")]
    assert c.delete("/api/memory/area").json()["forgotten"] is True
    assert c.get("/api/memory").json() == []


def test_bug_4_rebuild_reuses_database_connections(schemas):
    """BUGS.md #4: every settings save or reset opened new Database objects and never closed the old ones."""
    c, _ = client(schemas)
    gm = c.app.state.gm
    agent_db = gm.world.agent_db
    for n in range(3):
        c.put("/api/settings", json={"changes": {"limits.tool_calls_per_turn": 10 + n}})
    c.post("/api/reset", json={"scenario": "happy"})
    assert gm.world.agent_db is agent_db
    assert agent_db.scalar("select count(*) from pg_stat_activity where datname = current_database()") < 40
