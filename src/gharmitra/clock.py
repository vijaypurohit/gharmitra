"""Injectable clock (real and fake) so tests are reproducible.

Time is handled in UTC everywhere: `now()` is UTC, the database stores `timestamptz`, the provider
API and traces carry UTC ISO strings. `tz` is the user's timezone; it is used only where a local
calendar matters (what "today", "this week" and "mornings" mean, provider working hours) and when
showing a time to the user.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Test clock from design §12: Mon 2 Nov 2026, 09:00 IST (03:30 UTC).
TEST_START = datetime(2026, 11, 2, 9, 0, tzinfo=IST).astimezone(UTC)


class Clock(Protocol):
    tz: ZoneInfo  # the user's timezone, for display and local-calendar logic only

    def now(self) -> datetime: ...  # always UTC

    def sleep(self, seconds: float) -> None: ...


def utc_iso(dt: datetime) -> str:
    """The one string form for times crossing an interface: UTC, ISO 8601."""
    return dt.astimezone(UTC).isoformat()


def local(dt: datetime, clock: Clock) -> datetime:
    return dt.astimezone(clock.tz)


def local_today(clock: Clock) -> date:
    return clock.now().astimezone(clock.tz).date()


class SystemClock:
    def __init__(self, tz: ZoneInfo = IST) -> None:
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Time moves only when told to. `sleep` advances it, so backoff counts against time budgets."""

    def __init__(self, start: datetime = TEST_START, tz: ZoneInfo = IST) -> None:
        self.tz = tz
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.advance(seconds=seconds)

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)
