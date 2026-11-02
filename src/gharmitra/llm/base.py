"""Model protocol and Intent schema.

The model only turns a message into an Intent. The schema has no field for a price, an approval or a
tool call, and rejects unknown fields, so a model cannot express any of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Act = Literal["request", "inform", "select", "confirm", "decline", "memory", "status", "unclear"]
Goal = Literal["book", "reschedule", "cancel"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DateRange(_Strict):
    from_: date = Field(alias="from")
    to: date


class TimeWindow(_Strict):
    earliest: time | None = None
    latest: time | None = None


class ProviderRef(_Strict):
    same_as_last: bool = False
    name: str | None = None


class Entities(_Strict):
    service: str | None = None
    area: str | None = None
    date: DateRange | None = None
    time: TimeWindow | None = None
    budget_max: int | None = Field(default=None, ge=0, description="rupees")
    provider: ProviderRef | None = None
    no_preference: bool = False

    def given(self) -> set[str]:
        """Names of the fields this message set."""
        return {k for k, v in self.model_dump(exclude_defaults=True).items() if v not in (None, False)}


class Target(_Strict):
    service: str | None = None
    booking_id: str | None = None


class MemoryOp(_Strict):
    op: Literal["view", "correct", "forget"]
    key: str | None = None
    value: str | None = None


class Intent(_Strict):
    act: Act
    goal: Goal | None = None
    entities: Entities = Field(default_factory=Entities)
    target: Target | None = None
    option: int | None = Field(default=None, ge=1, le=8)
    memory: MemoryOp | None = None
    remember: bool = False
    confidence: float = Field(default=1.0, ge=0, le=1)


def intent_schema() -> dict[str, Any]:
    return Intent.model_json_schema(by_alias=True)


@dataclass
class Prompt:
    system: str
    user: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass
class ModelResult:
    text: str
    usage: Usage = field(default_factory=Usage)


class ModelError(Exception):
    """A failed model call. `kind` is one of: timeout, rate_limited, bad_output, process_error, http_error."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind


class ModelClient(Protocol):
    adapter: str
    model: str

    def complete(self, prompt: Prompt) -> ModelResult: ...
