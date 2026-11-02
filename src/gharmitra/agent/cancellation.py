"""Cancellation policy (design §10): fee tiers, per-provider overrides and the snapshot on each booking.

Platform default: ≥ 24 h before the slot free; 3–24 h a flat ₹100; < 3 h 50% of the price; once the
slot has started, no cancellation. A provider cancelling refunds in full plus a ₹100 credit. Each
booking keeps a snapshot of the policy in force when it was made, so later edits don't change it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any

from gharmitra.clock import Clock
from gharmitra.db import Database, jsonb
from gharmitra.money import format_inr, rupees

PLATFORM_KEY = "cancellation_policy"
OVERRIDES_KEY = "cancellation_overrides"


@dataclass(frozen=True)
class Policy:
    free_hours: float = 24.0
    late_hours: float = 3.0
    late_fee: int = rupees(100)  # paise
    very_late_percent: int = 50
    provider_cancel_credit: int = rupees(100)  # paise
    reschedule_free_hours: float = 3.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, base: Policy | None = None) -> Policy:
        """Known keys from `data` over `base` (default: the platform default). Unknown keys are ignored."""
        merged = (base or cls()).to_dict()
        names = {f.name for f in fields(cls)}
        merged.update({k: v for k, v in (data or {}).items() if k in names and v is not None})
        return cls(**merged)

    def validate(self) -> None:
        if not (0 <= self.late_hours <= self.free_hours):
            raise ValueError("late_hours must be between 0 and free_hours")
        if self.late_fee < 0 or self.provider_cancel_credit < 0:
            raise ValueError("fees and credits must be zero or more")
        if not 0 <= self.very_late_percent <= 100:
            raise ValueError("very_late_percent must be 0–100")


@dataclass(frozen=True)
class Quote:
    tier: str  # free | late | very_late | started
    fee: int  # paise
    refund: int  # paise
    hours_before: float

    @property
    def allowed(self) -> bool:
        return self.tier != "started"

    def describe(self, policy: Policy) -> str:
        if self.tier == "free":
            return f"No fee ({policy.free_hours:g} h or more before the slot)"
        if self.tier == "late":
            return f"Fee {format_inr(self.fee)} ({policy.late_hours:g}–{policy.free_hours:g} h before the slot)"
        if self.tier == "very_late":
            return f"Fee {format_inr(self.fee)} ({policy.very_late_percent}%, under {policy.late_hours:g} h before the slot)"
        return "The slot has already started"


def quote(policy: Policy, price: int, slot: datetime, now: datetime) -> Quote:
    """Boundaries: exactly `free_hours` before is free; exactly `late_hours` before is the flat fee."""
    hours = (slot - now).total_seconds() / 3600
    if hours >= policy.free_hours:
        fee, tier = 0, "free"
    elif hours >= policy.late_hours:
        fee, tier = min(policy.late_fee, price), "late"
    elif hours > 0:
        fee, tier = price * policy.very_late_percent // 100, "very_late"
    else:
        return Quote("started", price, 0, hours)
    return Quote(tier, fee, price - fee, hours)


def can_reschedule(policy: Policy, slot: datetime, now: datetime) -> bool:
    return (slot - now).total_seconds() / 3600 >= policy.reschedule_free_hours


class PolicyStore:
    """Platform policy and provider overrides, kept in the agent's `settings` table."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def _get(self, key: str) -> Any:
        return self.db.scalar("select value from settings where key = %s", (key,))

    def _put(self, key: str, value: Any) -> None:
        self.db.execute(
            "insert into settings (key, value, updated_at) values (%s, %s, %s)"
            " on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at",
            (key, jsonb(value), self.clock.now()),
        )

    def platform(self) -> Policy:
        return Policy.from_dict(self._get(PLATFORM_KEY))

    def overrides(self) -> dict[str, dict[str, Any]]:
        return self._get(OVERRIDES_KEY) or {}

    def set_platform(self, policy: Policy) -> None:
        policy.validate()
        self._put(PLATFORM_KEY, policy.to_dict())

    def set_override(self, provider_id: str, changes: dict[str, Any] | None) -> None:
        """Partial override for one provider; None removes it."""
        current = self.overrides()
        if changes:
            Policy.from_dict(changes, self.platform()).validate()
            current[provider_id] = changes
        else:
            current.pop(provider_id, None)
        self._put(OVERRIDES_KEY, current)

    def for_provider(self, provider_id: str) -> Policy:
        """Provider override beats the platform default."""
        return Policy.from_dict(self.overrides().get(provider_id), self.platform())

    def snapshot(self, provider_id: str) -> dict[str, Any]:
        return {**self.for_provider(provider_id).to_dict(), "provider_id": provider_id}

    def for_booking(self, snapshot: dict[str, Any] | None, provider_id: str) -> Policy:
        """The policy a booking was made under; older rows without a snapshot use today's."""
        return Policy.from_dict(snapshot) if snapshot else self.for_provider(provider_id)
