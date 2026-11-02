"""Tracer: numbered, redacted events per session, to Postgres trace_events and traces/<session_id>.jsonl."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Protocol

from gharmitra.clock import Clock
from gharmitra.db import Database, jsonb

REDACTED = "[redacted]"
_SECRET_KEY = re.compile(r"(api[_-]?key|authorization|token|secret|password|cookie)", re.I)
_PATTERNS = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    (re.compile(r"\b(sk|sk-ant|xai|gsk|AIza)[-_A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)[^@\s]+@"), r"\1" + REDACTED + "@"),
    # Word boundaries on both sides: a bare digit check also matched digit runs inside hex
    # fingerprints and ids, corrupting them in the trace (docs/BUGS.md #2).
    (re.compile(r"(?<![\w-])(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\w-])"), "[phone]"),
]


class Redactor:
    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = sorted({s for s in (secrets or []) if s}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, REDACTED)
        for pattern, repl in _PATTERNS:
            value = pattern.sub(repl, value)
        return value

    def __call__(self, value: Any, key: str = "") -> Any:
        if key and _SECRET_KEY.search(key):
            return REDACTED
        if key == "phone":
            return "[phone]"
        if key == "address":
            return "[address]"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        return value


class TraceSink(Protocol):
    def write(self, event: dict[str, Any]) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class JsonlSink:
    """traces/<session_id>.jsonl, plus traces/latest.jsonl for `tail -f`."""

    def __init__(self, directory: Path, latest: bool = True) -> None:
        self.directory = directory
        self.latest = latest
        self._started: set[str] = set()

    def write(self, event: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
        sid = event["session_id"]
        with open(self.directory / f"{sid}.jsonl", "a", encoding="utf-8") as f:
            f.write(line)
        if self.latest:
            mode = "a" if sid in self._started else "w"
            self._started.add(sid)
            with open(self.directory / "latest.jsonl", mode, encoding="utf-8") as f:
                f.write(line)


class PostgresSink:
    def __init__(self, db: Database) -> None:
        self.db = db

    def write(self, event: dict[str, Any]) -> None:
        self.db.execute(
            "insert into trace_events (session_id, seq, ts, type, payload) values (%s, %s, %s, %s, %s)",
            (event["session_id"], event["seq"], event["ts"], event["type"], jsonb(event["payload"])),
        )


class Tracer:
    """One trace per session. Sequence numbers continue across restarts when `start_seq` is given."""

    def __init__(
        self,
        session_id: str,
        sinks: list[TraceSink],
        clock: Clock,
        redactor: Redactor | None = None,
        start_seq: int = 0,
    ) -> None:
        self.session_id = session_id
        self.sinks = sinks
        self.clock = clock
        self.redact = redactor or Redactor()
        self.seq = start_seq
        self._lock = threading.Lock()

    def emit(self, type: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            self.seq += 1
            event = {
                "session_id": self.session_id,
                "seq": self.seq,
                "ts": self.clock.now().isoformat(),
                "type": type,
                "payload": self.redact(payload),
            }
            for sink in self.sinks:
                sink.write(event)
        return event


def next_seq(db: Database, session_id: str) -> int:
    return db.scalar("select coalesce(max(seq), 0) from trace_events where session_id = %s", (session_id,))
