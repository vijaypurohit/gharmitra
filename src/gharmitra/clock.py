"""Injectable clock (real and fake) so tests are reproducible."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Test clock from design §12: Mon 2 Nov 2026, 09:00 IST.
TEST_START = datetime(2026, 11, 2, 9, 0, tzinfo=IST)


class Clock(Protocol):
    tz: ZoneInfo

    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def __init__(self, tz: ZoneInfo = IST) -> None:
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Time moves only when told to. `sleep` advances it, so backoff counts against time budgets."""

    def __init__(self, start: datetime = TEST_START) -> None:
        self.tz = start.tzinfo  # type: ignore[assignment]
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.advance(seconds=seconds)

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)
