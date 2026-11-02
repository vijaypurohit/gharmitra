"""Approval records, fingerprinting and the approval gate.

An approval is a database row. Only a user turn can grant it, and `check()` recomputes the
fingerprint from the values about to be sent, right before every side-effecting call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gharmitra.clock import Clock
from gharmitra.db import Database
from gharmitra.trace import Tracer

ACTIONS = ("book", "cancel", "reschedule")


@dataclass(frozen=True)
class Values:
    """What a side-effecting call is about to do. Amount is paise: quoted price (book), current price
    (reschedule) or refund (cancel)."""

    action: str
    provider_id: str
    slot: str
    amount: int
    booking_id: str | None = None
    hold_id: str | None = None  # sent with `book`, not part of the fingerprint
    # Not part of the fingerprint: what the agent's booking copy needs (user_id, provider_name, service, area).
    meta: dict = field(default_factory=dict, compare=False, hash=False)


def _utc(slot: str) -> str:
    return datetime.fromisoformat(slot).astimezone(UTC).isoformat()


def fingerprint(v: Values) -> str:
    canonical = json.dumps(
        [v.action, v.provider_id, _utc(v.slot), int(v.amount), v.booking_id or ""], separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalError(Exception):
    """The gate refused. `reason`: missing, expired, not_granted, mismatch."""

    def __init__(self, reason: str, approval_id: str | None = None) -> None:
        super().__init__(f"{reason} ({approval_id})")
        self.reason = reason
        self.approval_id = approval_id


class Approvals:
    def __init__(self, db: Database, clock: Clock, tracer: Tracer) -> None:
        self.db = db
        self.clock = clock
        self.tracer = tracer

    def _event(self, row: dict, **extra: Any) -> None:
        self.tracer.emit(
            "approval", approval_id=row["id"], status=row["status"], action=row["action"],
            provider_id=row["provider_id"], slot=row["slot_start"], amount=row["amount"],
            booking_id=row["booking_id"], fingerprint=row["fingerprint"], **extra,
        )

    def create(
        self, *, session_id: str, user_id: str, values: Values, provider_name: str, summary: str, expires_at: datetime
    ) -> dict:
        approval_id = "apr_%d" % self.db.scalar("select nextval('approval_seq')")
        row = self.db.one(
            "insert into approvals (id, session_id, user_id, action, provider_id, provider_name, slot_start, amount,"
            " booking_id, hold_id, fingerprint, status, summary, expires_at, created_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s) returning *",
            (approval_id, session_id, user_id, values.action, values.provider_id, provider_name,
             datetime.fromisoformat(values.slot), values.amount, values.booking_id, values.hold_id,
             fingerprint(values), summary, expires_at, self.clock.now()),
        )
        self._event(row)
        return row

    def get(self, approval_id: str | None) -> dict | None:
        if not approval_id:
            return None
        return self.db.one("select * from approvals where id = %s", (approval_id,))

    def _move(self, approval_id: str, to: str, allowed: tuple[str, ...], reason: str = "", **sets: Any) -> dict | None:
        assignments = "".join(f", {k} = %({k})s" for k in sets)
        row = self.db.one(
            f"update approvals set status = %(to)s, status_reason = %(reason)s{assignments}"
            " where id = %(id)s and status = any(%(allowed)s) returning *",
            {"to": to, "reason": reason or None, "id": approval_id, "allowed": list(allowed), **sets},
        )
        if row is not None:
            self._event(row, reason=reason or None)
        return row

    def is_expired(self, row: dict) -> bool:
        return self.clock.now() > row["expires_at"]

    def grant(self, approval_id: str, message_id: int) -> dict | None:
        """Only from a user turn (`message_id` is that user message) while the approval is pending."""
        row = self.get(approval_id)
        if row is None or row["status"] != "pending":
            return None
        if self.is_expired(row):
            self.expire(approval_id)
            return None
        return self._move(approval_id, "granted", ("pending",), "user_turn",
                          granted_at=self.clock.now(), granted_by_message=message_id)

    def void(self, approval_id: str | None, reason: str) -> dict | None:
        if not approval_id:
            return None
        return self._move(approval_id, "voided", ("pending", "granted"), reason)

    def expire(self, approval_id: str | None, reason: str = "expired") -> dict | None:
        if not approval_id:
            return None
        return self._move(approval_id, "expired", ("pending", "granted"), reason)

    def consume(self, approval_id: str) -> dict | None:
        return self._move(approval_id, "consumed", ("granted",), "succeeded")

    def check(self, approval_id: str | None, values: Values) -> dict:
        """The gate. Raises ApprovalError unless the approval exists, is granted, unexpired, and its
        fingerprint equals the fingerprint of the values about to be sent."""
        row = self.get(approval_id)
        fp = fingerprint(values)
        reason = None
        if row is None:
            reason = "missing"
        elif row["status"] != "granted":
            reason = "not_granted"
        elif self.is_expired(row):
            self.expire(row["id"])
            reason = "expired"
        elif row["fingerprint"] != fp or row["action"] != values.action:
            reason = "mismatch"
        self.tracer.emit("gate", approval_id=approval_id, action=values.action, fingerprint=fp,
                         allowed=reason is None, reason=reason)
        if reason:
            raise ApprovalError(reason, approval_id)
        return row  # type: ignore[return-value]
