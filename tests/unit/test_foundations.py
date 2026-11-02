import json

import psycopg
import pytest

from gharmitra.catalogue import load_catalogue
from gharmitra.clock import TEST_START, FakeClock
from gharmitra.config import load_settings
from gharmitra.db import migrate
from gharmitra.money import format_inr, rupees
from gharmitra.trace import JsonlSink, MemorySink, PostgresSink, Redactor, Tracer, next_seq


# --- config -----------------------------------------------------------------


def test_config_runs_without_env(tmp_path):
    s = load_settings(env={}, dotenv=tmp_path / "missing.env")
    assert s.model_adapter == "rules"
    assert s.database_url is None
    assert s.limits.tool_calls_per_turn == 15


def test_config_reads_dotenv_and_env_wins(tmp_path):
    f = tmp_path / ".env"
    f.write_text("MODEL_ADAPTER=openai\nSEED=7  # comment\nMODEL_API_KEY='sk-abc12345'\n")
    s = load_settings(env={"SEED": "9"}, dotenv=f)
    assert (s.model_adapter, s.seed, s.model_api_key) == ("openai", 9, "sk-abc12345")
    assert "sk-abc12345" not in repr(s)


def test_secret_values_include_db_password():
    s = load_settings(env={"DATABASE_URL": "postgresql://u:hunter22@db/x", "MODEL_API_KEY": "key-12345"}, dotenv=None)
    assert set(s.secret_values()) == {"hunter22", "key-12345"}


# --- clock and money ----------------------------------------------------------


def test_fake_clock_moves_only_when_told():
    c = FakeClock()
    assert c.now() == TEST_START
    c.sleep(2.5)
    c.advance(minutes=10)
    assert (c.now() - TEST_START).total_seconds() == 602.5


@pytest.mark.parametrize(
    "paise, text",
    [(rupees(1200), "₹1,200"), (rupees(120000), "₹1,20,000"), (rupees(950), "₹950"),
     (rupees(12345678), "₹1,23,45,678"), (120050, "₹1,200.50"), (-rupees(100), "-₹100")],
)
def test_format_inr(paise, text):
    assert format_inr(paise) == text


# --- catalogue ------------------------------------------------------------------


def test_catalogue_aliases():
    cat = load_catalogue()
    assert cat.area("HSR") == "HSR Layout"
    assert cat.area("kormangala") == "Koramangala"
    assert cat.area("Mars") is None
    assert cat.service("AC service") == "ac_servicing"
    assert cat.find_service("Find a plumber for my kitchen tap") == "plumber"
    assert cat.find_service("Move my AC servicing to Saturday") == "ac_servicing"
    assert cat.find_area("plumber in hsr tomorrow") == "HSR Layout"


# --- database ---------------------------------------------------------------------


def test_bundled_postgres_and_injected_schemas(schemas):
    assert schemas.agent.scalar("select current_schema()") == schemas.agent.schema
    assert schemas.mock.scalar("select current_schema()") == schemas.mock.schema
    assert schemas.agent.scalar("select count(*) from bookings") == 0


def test_migrations_idempotent(schemas):
    assert migrate(schemas.agent, "agent") == []
    assert migrate(schemas.mock, "mock") == []


def test_second_active_booking_for_same_slot_rejected(schemas, clock):
    db = schemas.agent
    db.execute("insert into users (id, name) values ('u1', 'U')")
    sql = (
        "insert into bookings (id, user_id, provider_id, provider_name, service, slot_start, price, status,"
        " created_at, updated_at) values (%s, 'u1', 'P1', 'P', 'plumber', %s, 100, %s, %s, %s)"
    )
    now = clock.now()
    db.execute(sql, ("BK-1", now, "booked", now, now))
    db.execute(sql, ("BK-0", now, "cancelled", now, now))  # inactive rows don't count
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(sql, ("BK-2", now, "booked", now, now))


# --- tracer -----------------------------------------------------------------------


def test_redaction():
    r = Redactor(["supersecretvalue"])
    out = r({
        "api_key": "abc", "headers": {"Authorization": "Bearer xyz"},
        "note": "key supersecretvalue, call +91 98450 12345 or 9845012345",
        "url": "postgresql://app:pw123@localhost/db", "phone": "080-1234", "address": "12 MG Road",
        "booking": "BK-1042", "slot": "2026-11-05T09:00:00+05:30", "amount": 120000,
    })
    assert out["api_key"] == out["headers"]["Authorization"] == "[redacted]"
    assert "supersecretvalue" not in out["note"] and out["note"].count("[phone]") == 2
    assert out["url"] == "postgresql://app:[redacted]@localhost/db"
    assert (out["phone"], out["address"]) == ("[phone]", "[address]")
    assert (out["booking"], out["slot"], out["amount"]) == ("BK-1042", "2026-11-05T09:00:00+05:30", 120000)


def test_tracer_sinks_and_contiguous_seq(schemas, clock, traces_dir):
    mem = MemorySink()
    t = Tracer("s1", [mem, JsonlSink(traces_dir), PostgresSink(schemas.agent)], clock, Redactor(["topsecret1"]))
    t.emit("user_msg", text="hi topsecret1")
    t.emit("intent", act="request")
    assert [e["seq"] for e in mem.events] == [1, 2]
    lines = [json.loads(x) for x in (traces_dir / "s1.jsonl").read_text().splitlines()]
    assert [e["type"] for e in lines] == ["user_msg", "intent"]
    assert "topsecret1" not in (traces_dir / "s1.jsonl").read_text()
    assert (traces_dir / "latest.jsonl").read_text() == (traces_dir / "s1.jsonl").read_text()
    # A restarted tracer continues the numbering.
    t2 = Tracer("s1", [mem, PostgresSink(schemas.agent)], clock, start_seq=next_seq(schemas.agent, "s1"))
    t2.emit("exit", reason="completed")
    assert schemas.agent.scalar("select array_agg(seq order by seq) from trace_events") == [1, 2, 3]


def test_bug_1_bundled_postgres_path_with_spaces():
    """BUGS.md #1: pgserver passes the socket dir unquoted; a data dir with a space failed to start."""
    import shutil
    import tempfile
    from pathlib import Path

    from pgserver import postgres_server

    from gharmitra.db import _patch_socket_dir

    root = Path(tempfile.mkdtemp(prefix="gm"))  # short, so the socket name fits the OS limit
    try:
        pgdata = root / "a b" / "pg"
        pgdata.mkdir(parents=True)
        _patch_socket_dir()
        chosen = postgres_server.find_suitable_socket_dir(pgdata, root / "run")
        assert not any(c.isspace() for c in str(chosen))
    finally:
        shutil.rmtree(root)
