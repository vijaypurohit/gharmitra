"""Hard caps on turns, model calls, tool calls and wall-clock time.

Every model and tool call goes through `spend()` first. A call that would go over a cap is not
made; `LimitExceeded` carries the exit reason the controller reports.
"""

from __future__ import annotations

from gharmitra.clock import Clock
from gharmitra.config import Limits


class LimitExceeded(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class Budget:
    """Per-turn budget. The session turn cap is checked once at the start of the turn."""

    def __init__(self, limits: Limits, clock: Clock) -> None:
        self.limits = limits
        self.clock = clock
        self.started = clock.now()
        self.model_calls = 0
        self.tool_calls = 0

    @property
    def elapsed(self) -> float:
        return (self.clock.now() - self.started).total_seconds()

    def check_time(self) -> None:
        if self.elapsed > self.limits.seconds_per_turn:
            raise LimitExceeded("limit_time", f"{self.elapsed:.0f}s > {self.limits.seconds_per_turn:.0f}s")

    def spend(self, kind: str) -> None:
        self.check_time()
        if kind == "model":
            if self.model_calls + 1 > self.limits.model_calls_per_turn:
                raise LimitExceeded("limit_model_calls", f"cap {self.limits.model_calls_per_turn}")
            self.model_calls += 1
        elif kind == "tool":
            if self.tool_calls + 1 > self.limits.tool_calls_per_turn:
                raise LimitExceeded("limit_tool_calls", f"cap {self.limits.tool_calls_per_turn}")
            self.tool_calls += 1
        else:
            raise ValueError(kind)

    def check_turns(self, turns: int) -> None:
        if turns > self.limits.turns_per_session:
            raise LimitExceeded("limit_turns", f"cap {self.limits.turns_per_session}")

    def snapshot(self) -> dict:
        return {"model_calls": self.model_calls, "tool_calls": self.tool_calls, "elapsed_s": round(self.elapsed, 3)}
