"""Validate and normalise a parsed Intent against the city catalogue; low confidence or unknown values become a clarifying question.

`IntentParser.parse` is the only path from user text to an Intent: it builds the prompt, calls the
configured model through the budget, falls back to `rules` when that call fails, traces every call
and normalises the result.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import date, timedelta

from pydantic import ValidationError

from gharmitra.agent.budget import Budget
from gharmitra.catalogue import Catalogue
from gharmitra.llm.base import Intent, ModelClient, ModelError
from gharmitra.llm.prompt import build_prompt
from gharmitra.trace import Tracer

MAX_DAYS_AHEAD = 30


@dataclass
class Parsed:
    intent: Intent
    problems: dict[str, str] = field(default_factory=dict)  # field → what's wrong, e.g. {"area": "Mars"}
    usable: bool = True
    adapter: str = ""
    fallback: bool = False


def normalise(intent: Intent, catalogue: Catalogue, today: date) -> tuple[Intent, dict[str, str]]:
    """Canonical service and area names; dates in [today, today + 30]. Unknown values are dropped and
    reported, never guessed."""
    intent = intent.model_copy(deep=True)
    problems: dict[str, str] = {}
    e = intent.entities
    if e.service is not None:
        canonical = catalogue.service(e.service)
        if canonical is None:
            problems["service"] = e.service
        e.service = canonical
    if e.area is not None:
        canonical = catalogue.area(e.area)
        if canonical is None:
            problems["area"] = e.area
        e.area = canonical
    if intent.target and intent.target.service:
        canonical = catalogue.service(intent.target.service)
        if canonical is None:
            problems["service"] = intent.target.service
        intent.target.service = canonical
    if e.date is not None:
        last_ok = today + timedelta(days=MAX_DAYS_AHEAD)
        start, end = sorted((e.date.from_, e.date.to))
        if end < today:
            problems["date"] = "past"
            e.date = None
        elif start > last_ok:
            problems["date"] = "too_far"
            e.date = None
        else:
            e.date.from_, e.date.to = max(start, today), min(end, last_ok)
    if e.time and e.time.earliest and e.time.latest and e.time.earliest >= e.time.latest:
        problems["time"] = "empty_window"
        e.time = None
    return intent, problems


class IntentParser:
    def __init__(
        self,
        model: ModelClient,
        fallback: ModelClient | None,
        catalogue: Catalogue,
        tracer: Tracer,
        min_confidence: float = 0.6,
    ) -> None:
        self.model = model
        self.fallback = fallback if fallback is not None and fallback.adapter != model.adapter else None
        self.catalogue = catalogue
        self.tracer = tracer
        self.min_confidence = min_confidence

    def parse(
        self,
        text: str,
        *,
        today: date,
        phase: str,
        known_fields: list[str],
        awaiting: str | None,
        budget: Budget,
        options_shown: int = 0,
    ) -> Parsed:
        prompt = build_prompt(
            text, today=today, catalogue=self.catalogue, phase=phase, known_fields=known_fields,
            awaiting=awaiting, options_shown=options_shown,
        )
        clients = [self.model] + ([self.fallback] if self.fallback else [])
        intent: Intent | None = None
        used = ""
        for i, client in enumerate(clients):
            budget.spend("model")
            started = _time.perf_counter()
            error: str | None = None
            usage = None
            try:
                result = client.complete(prompt)
                usage = result.usage
                intent = Intent.model_validate_json(result.text)
            except ModelError as exc:
                error = exc.kind
            except ValidationError:
                error = "bad_output"
            self.tracer.emit(
                "model_call",
                adapter=client.adapter,
                model=client.model,
                latency_ms=round((_time.perf_counter() - started) * 1000, 1),
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                cost_usd=usage.cost_usd if usage else None,
                fallback=i > 0,
                error=error,
            )
            if intent is not None:
                used = client.adapter
                break
        if intent is None:
            return Parsed(Intent(act="unclear", confidence=0.0), usable=False, adapter=used, fallback=True)
        intent, problems = normalise(intent, self.catalogue, today)
        usable = intent.act != "unclear" and intent.confidence >= self.min_confidence
        return Parsed(intent, problems, usable, used, fallback=used != self.model.adapter)
