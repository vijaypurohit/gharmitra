"""Shared fixtures: fake clock, bundled Postgres, fresh schemas per test, seeded mock API, traces in a temp dir."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from gharmitra.clock import FakeClock
from gharmitra.config import load_settings
from gharmitra.db import Database, drop_schema, migrate, resolve_url


@pytest.fixture(scope="session")
def pg_url() -> str:
    settings = load_settings()
    return settings.test_database_url or resolve_url(settings.database_url, settings.data_dir)


@dataclass
class Schemas:
    agent: Database
    mock: Database


@pytest.fixture
def schemas(pg_url: str):
    prefix = f"t{uuid.uuid4().hex[:10]}"
    pair = Schemas(Database(pg_url, f"{prefix}_agent"), Database(pg_url, f"{prefix}_mock"))
    migrate(pair.agent, "agent")
    migrate(pair.mock, "mock")
    yield pair
    for db in (pair.agent, pair.mock):
        drop_schema(db)
    for db in list(Database.instances):  # worlds built in the test open their own connections
        db.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def traces_dir(tmp_path: Path) -> Path:
    return tmp_path / "traces"
