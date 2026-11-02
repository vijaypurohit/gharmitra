"""The only way the agent calls the provider API: budget first, then the call, then sanitise and trace.

Callers only ever see sanitised results; raw tool text stays inside this module.
"""

from __future__ import annotations

import time as _time
from typing import Any

from gharmitra.agent.budget import Budget
from gharmitra.agent.sanitize import Sanitizer
from gharmitra.clock import Clock
from gharmitra.mock_api.provider import ProviderAPI, ToolError
from gharmitra.trace import Tracer

TOOL_NAMES = ("search_providers", "get_provider", "list_bookings", "hold_slot", "book", "get_booking_status",
              "cancel", "reschedule")


class Tools:
    def __init__(self, api: ProviderAPI, budget: Budget, tracer: Tracer, clock: Clock) -> None:
        self.api = api
        self.budget = budget
        self.tracer = tracer
        self.clock = clock

    def call(self, name: str, **args: Any) -> Any:
        if name not in TOOL_NAMES:
            raise ValueError(f"unknown tool {name}")
        self.budget.spend("tool")
        sanitizer = Sanitizer()
        started, sim_started = _time.perf_counter(), self.clock.now()
        error: ToolError | None = None
        result: Any = None
        try:
            result = sanitizer.result(name, getattr(self.api, name)(**args))
        except ToolError as exc:
            error = exc
        latency = (_time.perf_counter() - started) * 1000 + (self.clock.now() - sim_started).total_seconds() * 1000
        self.tracer.emit(
            "tool_call",
            name=name,
            args=args,
            result=result,
            error=None if error is None else {"code": error.code, "message": sanitizer.error(str(error))},
            latency_ms=round(latency, 1),
        )
        for flag in sanitizer.flags:
            self.tracer.emit("injection_flagged", tool=name, **flag)
        if error is not None:
            raise error
        return result

    def __getattr__(self, name: str) -> Any:
        if name in TOOL_NAMES:
            return lambda **args: self.call(name, **args)
        raise AttributeError(name)
