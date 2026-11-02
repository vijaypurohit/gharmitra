"""Facts with provenance (stated/inferred), confirmation and forget tombstones.

Keys: `area`, `time_earliest` (HH:MM), `budget_max` (rupees), `provider:<service>` (provider id).
`fact_events` and trace `memory` events record key and operation, never the value.
"""

from __future__ import annotations

from gharmitra.clock import Clock
from gharmitra.db import Database
from gharmitra.trace import Tracer

KEYS_LABEL = {"area": "area", "time_earliest": "earliest time", "budget_max": "usual budget"}


def label(key: str) -> str:
    if key.startswith("provider:"):
        return f"preferred {key.split(':', 1)[1].replace('_', ' ')} provider"
    return KEYS_LABEL.get(key, key)


class MemoryStore:
    def __init__(self, db: Database, clock: Clock, tracer: Tracer | None = None) -> None:
        self.db = db
        self.clock = clock
        self.tracer = tracer

    def _log(self, user_id: str, key: str, op: str, status: str) -> None:
        self.db.execute(
            "insert into fact_events (user_id, key, op, created_at) values (%s, %s, %s, %s)",
            (user_id, key, op, self.clock.now()),
        )
        if self.tracer:
            self.tracer.emit("memory", key=key, op=op, status=status)

    def _upsert(self, user_id: str, key: str, value: str | None, status: str) -> None:
        self.db.execute(
            "insert into facts (user_id, key, value, status, updated_at) values (%s, %s, %s, %s, %s)"
            " on conflict (user_id, key) do update set value = excluded.value, status = excluded.status,"
            " updated_at = excluded.updated_at",
            (user_id, key, value, status, self.clock.now()),
        )

    def all(self, user_id: str) -> list[dict]:
        """Live facts (stated and inferred). Forgotten ones are never returned."""
        return self.db.all(
            "select key, value, status, updated_at from facts where user_id = %s and status <> 'forgotten'"
            " order by key",
            (user_id,),
        )

    def get(self, user_id: str, key: str) -> dict | None:
        return self.db.one("select key, value, status from facts where user_id = %s and key = %s", (user_id, key))

    def stated(self, user_id: str, key: str) -> str | None:
        row = self.get(user_id, key)
        return row["value"] if row and row["status"] == "stated" else None

    def state(self, user_id: str, key: str, value: str) -> None:
        with self.db.tx():
            self._upsert(user_id, key, value, "stated")
            self._log(user_id, key, "state", "stated")

    def correct(self, user_id: str, key: str, value: str) -> None:
        with self.db.tx():
            self._upsert(user_id, key, value, "stated")
            self._log(user_id, key, "correct", "stated")

    def infer(self, user_id: str, key: str, value: str) -> bool:
        """Records an inferred fact unless the key is stated or forgotten. Returns True if recorded."""
        row = self.get(user_id, key)
        if row is not None and row["status"] in ("stated", "forgotten"):
            return False
        if row is not None and row["value"] == value:
            return True
        with self.db.tx():
            self._upsert(user_id, key, value, "inferred")
            self._log(user_id, key, "infer", "inferred")
        return True

    def confirm(self, user_id: str, key: str) -> bool:
        """An inferred fact becomes stated after the user says yes."""
        with self.db.tx():
            n = self.db.execute(
                "update facts set status = 'stated', updated_at = %s where user_id = %s and key = %s"
                " and status = 'inferred'",
                (self.clock.now(), user_id, key),
            )
            if n:
                self._log(user_id, key, "confirm", "stated")
        return bool(n)

    def forget(self, user_id: str, key: str) -> bool:
        """Tombstone: value NULL, status forgotten. Inference skips the key until it is stated again."""
        row = self.get(user_id, key)
        if row is None or row["status"] == "forgotten":
            return False
        with self.db.tx():
            self._upsert(user_id, key, None, "forgotten")
            self._log(user_id, key, "forget", "forgotten")
        return True
