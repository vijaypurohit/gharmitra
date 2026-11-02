"""Builds the configured implementation of each interface (model, provider API, trace sinks, policy, renderers)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from gharmitra.agent.cancellation import PolicyStore
from gharmitra.agent.controller import Controller, Deps, Reply
from gharmitra.agent.followup import provider_cancelled
from gharmitra.catalogue import Catalogue, load_catalogue
from gharmitra.clock import Clock, SystemClock, local
from gharmitra.config import Limits, Settings, SettingsStore, apply_overrides, load_settings
from gharmitra.db import Database, jsonb, migrate, resolve_url
from gharmitra.llm.base import ModelClient
from gharmitra.llm.factory import make_model
from gharmitra.llm.rules import RuleModel
from gharmitra.mock_api import seed_data as sd
from gharmitra.mock_api.provider import MockProviderAPI
from gharmitra.mock_api.scenarios import Scenario, get_scenario
from gharmitra.trace import JsonlSink, MemorySink, PostgresSink, Redactor, TraceSink


@dataclass
class World:
    settings: Settings
    agent_db: Database
    mock_db: Database
    clock: Clock
    scenario: Scenario
    catalogue: Catalogue
    controller: Controller
    memory_sink: MemorySink | None = None
    crash_points: set[str] = field(default_factory=set)

    def api(self, user_id: str) -> MockProviderAPI:
        return MockProviderAPI(self.mock_db, self.clock, self.scenario, user_id, self.catalogue)

    def provider_cancels(self, booking_id: str) -> Reply | None:
        """Demo hook: the provider cancels, then the agent gets the event and follows up."""
        owner = self.mock_db.scalar("select user_id from bookings where id = %s", (booking_id,))
        if owner is None:
            return None
        self.api(owner).provider_cancels(booking_id)
        return provider_cancelled(self.controller, booking_id)

    def restart(self) -> Controller:
        """A new controller over the same database: what a process restart looks like."""
        self.controller = Controller(self.controller.deps)
        return self.controller


def prune_traces(db: Database, clock: Clock, days: int) -> int:
    """Drop trace events of sessions idle longer than the retention period (0 keeps everything)."""
    if days <= 0:
        return 0
    cutoff = clock.now() - timedelta(days=days)
    return db.scalar(
        "with old as (delete from trace_events where session_id in"
        " (select id from sessions where updated_at < %s) returning 1) select count(*) from old", (cutoff,))


def seed_agent(db: Database, clock: Clock, catalogue: Catalogue) -> None:
    """Users and the agent's copies of the seeded past bookings (the mock has the originals)."""
    if db.scalar("select count(*) from users"):
        return
    today = local(clock.now(), clock)  # seeded bookings sit at local wall-clock times
    policies = PolicyStore(db, clock)
    with db.tx():
        for uid, name in ((sd.DEMO_USER, sd.DEMO_USER_NAME), (sd.NEW_USER, "New user")):
            db.execute("insert into users (id, name) values (%s, %s)", (uid, name))
        names = {pid: name for rows in sd.PROVIDERS.values() for pid, name, *_ in rows}
        for n, b in enumerate(sd.PAST_BOOKINGS):
            db.execute(
                "insert into bookings (id, user_id, provider_id, provider_name, service, area, slot_start, price,"
                " status, policy, created_at, updated_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (f"BK-{901 + n}", b.user_id, b.provider_id, names[b.provider_id], b.service, b.area,
                 sd.past_slot(today, b), b.price * 100, b.status, jsonb(policies.snapshot(b.provider_id)),
                 today, today),
            )


def build(
    settings: Settings | None = None,
    *,
    url: str | None = None,
    agent_schema: str = "agent",
    mock_schema: str = "mock",
    clock: Clock | None = None,
    scenario: Scenario | None = None,
    model: ModelClient | None = None,
    sinks: list[TraceSink] | None = None,
    limits: Limits | None = None,
    crash_points: set[str] | None = None,
    dbs: tuple[Database, Database] | None = None,
) -> World:
    """`dbs` reuses open (agent, mock) databases, e.g. when the web app applies new settings."""
    settings = settings or load_settings()
    if dbs is not None:
        agent_db, mock_db = dbs
    else:
        url = url or resolve_url(settings.database_url, settings.data_dir)
        agent_db, mock_db = Database(url, agent_schema), Database(url, mock_schema)
    migrate(agent_db, "agent")
    migrate(mock_db, "mock")
    settings = apply_overrides(settings, SettingsStore(agent_db).get())  # defaults < env < Settings page
    clock = clock or SystemClock()
    prune_traces(agent_db, clock, settings.trace_retention_days)
    catalogue = load_catalogue(settings.city)
    scenario = scenario or get_scenario(settings.scenario, settings.seed)
    MockProviderAPI(mock_db, clock, scenario, sd.DEMO_USER, catalogue).seed()
    seed_agent(agent_db, clock, catalogue)

    memory_sink = None
    if sinks is None:
        sinks = []
        if "postgres" in settings.trace_sinks:
            sinks.append(PostgresSink(agent_db))
        if "jsonl" in settings.trace_sinks:
            sinks.append(JsonlSink(settings.traces_dir))
    model = model or make_model(settings)
    crash_points = crash_points if crash_points is not None else set()
    world: World
    deps = Deps(
        db=agent_db,
        clock=clock,
        catalogue=catalogue,
        model=model,
        fallback=RuleModel(catalogue),
        api_for=lambda user_id: world.api(user_id),
        sinks=sinks,
        limits=limits or settings.limits,
        redactor=Redactor(settings.secret_values()),
        crash_points=crash_points,
    )
    world = World(settings, agent_db, mock_db, clock, scenario, catalogue, Controller(deps), memory_sink, crash_points)
    return world
