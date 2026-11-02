"""Postgres connection, transactions and migrations (schemas `agent` and `mock`, names injectable for tests).

Each `Database` is bound to one schema through `search_path`, so the agent and the mock never see
each other's tables. Connections are per thread and autocommit; `tx()` opens a transaction.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb, set_json_dumps

set_json_dumps(lambda obj: json.dumps(obj, default=str))

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_server: Any = None
_server_lock = threading.Lock()


def _patch_socket_dir() -> None:
    """pgserver passes the socket dir to postgres unquoted (`-o "-k <dir>"`), and by default that dir
    is the data dir. A data dir with a space (".../DEV Projects/...") then fails to start. Fall back
    to pgserver's own runtime dir whenever the chosen path contains whitespace. See docs/BUGS.md #1."""
    from pgserver import postgres_server

    original = postgres_server.find_suitable_socket_dir
    if getattr(original, "_gharmitra", False):
        return

    def no_whitespace(pgdata: Path, runtime_path: Path) -> Path:
        path = original(pgdata, runtime_path)
        if any(c.isspace() for c in str(path)):
            import hashlib

            path = Path(runtime_path) / hashlib.sha256(str(pgdata).encode()).hexdigest()[:10]
            path.mkdir(parents=True, exist_ok=True)
        if any(c.isspace() for c in str(path)):
            raise RuntimeError(f"no socket directory without spaces available (tried {path})")
        return path

    no_whitespace._gharmitra = True  # type: ignore[attr-defined]
    postgres_server.find_suitable_socket_dir = no_whitespace


def bundled_url(data_dir: Path) -> str:
    """Start (or reuse) the bundled Postgres under `data_dir/pg` and return its URL."""
    global _server
    with _server_lock:
        if _server is None:
            import pgserver

            _patch_socket_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            _server = pgserver.get_server(str((data_dir / "pg").resolve()), cleanup_mode="stop")
        return _server.get_uri()


def resolve_url(database_url: str | None, data_dir: Path) -> str:
    return database_url or bundled_url(data_dir)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


class Database:
    def __init__(self, url: str, schema: str) -> None:
        if not _SCHEMA_NAME.match(schema):
            raise ValueError(f"bad schema name: {schema!r}")
        self.url = url
        self.schema = schema
        self._local = threading.local()
        self._all: list[psycopg.Connection] = []
        self._lock = threading.Lock()

    @property
    def conn(self) -> psycopg.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = psycopg.connect(
                self.url,
                autocommit=True,
                row_factory=dict_row,
                options=f"-c search_path={self.schema} -c timezone=UTC",
            )
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    @contextlib.contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """A transaction (a savepoint when nested). Commits on exit, rolls back on error."""
        with self.conn.transaction():
            yield self.conn

    def execute(self, sql: str, params: Any = None) -> int:
        return self.conn.execute(sql, params).rowcount

    def one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Any = None) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else next(iter(row.values()))

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                conn.close()
            self._all.clear()


def migrate(db: Database, kind: str) -> list[str]:
    """Apply `migrations/<kind>/*.sql` to `db.schema` once each. Returns the versions applied."""
    folder = resources.files("gharmitra.migrations").joinpath(kind)
    files = sorted((f for f in folder.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    applied: list[str] = []
    with db.tx() as conn:
        # Serialise concurrent migrators (web + CLI on the same database).
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (db.schema,))
        conn.execute(f'create schema if not exists "{db.schema}"')
        conn.execute(
            "create table if not exists schema_migrations"
            " (version text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["version"] for r in conn.execute("select version from schema_migrations").fetchall()}
        for f in files:
            if f.name in done:
                continue
            conn.execute(f.read_text())
            conn.execute("insert into schema_migrations (version) values (%s)", (f.name,))
            applied.append(f.name)
    return applied


def drop_schema(db: Database) -> None:
    db.execute(f'drop schema if exists "{db.schema}" cascade')
