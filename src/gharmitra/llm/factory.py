"""Adapter registry: built-in names and `module:Class` import paths; pacing and fallback to rules."""

from __future__ import annotations

import importlib
import logging
import time
from datetime import date

from gharmitra.config import Settings
from gharmitra.llm.base import ModelClient
from gharmitra.llm.rules import RuleModel

log = logging.getLogger(__name__)


BUILTIN = {"openai": "gharmitra.llm.openai_api:OpenAIModel",
           "anthropic": "gharmitra.llm.anthropic_api:AnthropicModel",
           "claude_cli": "gharmitra.llm.claude_cli:ClaudeCliModel"}


class Paced:
    """Keeps at least `interval` seconds between calls (free tiers limit requests per minute)."""

    def __init__(self, inner: ModelClient, interval: float, sleep=time.sleep, now=time.monotonic) -> None:
        self.inner, self.interval, self.sleep, self.now = inner, interval, sleep, now
        self.adapter, self.model = inner.adapter, inner.model
        self.last: float | None = None

    def complete(self, prompt):  # type: ignore[no-untyped-def]
        if self.last is not None and (wait := self.last + self.interval - self.now()) > 0:
            self.sleep(wait)
        try:
            return self.inner.complete(prompt)
        finally:
            self.last = self.now()


def _load(path: str):  # type: ignore[no-untyped-def]
    module, _, cls = path.partition(":")
    return getattr(importlib.import_module(module), cls)


def make_model(settings: Settings, *, strict: bool = False) -> ModelClient:
    """The configured adapter. A misconfigured one (e.g. no MODEL_NAME) logs a warning and falls back
    to the rules parser, unless `strict`."""
    name = settings.model_adapter
    if name == "rules":
        return RuleModel()
    path = name if ":" in name else BUILTIN.get(name)
    if path is None:
        raise ValueError(f"unknown MODEL_ADAPTER {name!r}; use rules, {', '.join(BUILTIN)} or module:Class")
    try:
        model = _load(path)(settings)
    except (ValueError, ImportError) as exc:
        if strict:
            raise
        log.warning("model adapter %s unavailable (%s); using rules", name, exc)
        return RuleModel()
    if settings.model_min_interval_s > 0:
        return Paced(model, settings.model_min_interval_s)  # type: ignore[return-value]
    return model


def probe(settings: Settings, text: str, today: date | None = None) -> dict:
    """Parse one message with the configured model: intent, latency, tokens, cost (or the error)."""
    from gharmitra.catalogue import load_catalogue
    from gharmitra.llm.base import Intent, ModelError
    from gharmitra.llm.prompt import build_prompt

    try:
        model = make_model(settings, strict=True)
    except (ValueError, ImportError) as exc:
        return {"adapter": settings.model_adapter, "model": settings.model_name, "ok": False,
                "error": "config", "detail": str(exc)}
    prompt = build_prompt(text, today=today or date.today(), catalogue=load_catalogue(settings.city),
                          phase="COLLECTING", known_fields=[], awaiting=None)
    out: dict = {"adapter": model.adapter, "model": model.model}
    started = time.perf_counter()
    try:
        result = model.complete(prompt)
        intent = Intent.model_validate_json(result.text)
    except ModelError as exc:
        return {**out, "ok": False, "error": exc.kind, "detail": str(exc)[:300],
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except ValueError as exc:  # pydantic ValidationError and JSON errors
        return {**out, "ok": False, "error": "bad_output", "detail": str(exc)[:300],
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    return {**out, "ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
            "cost_usd": result.usage.cost_usd,
            "intent": intent.model_dump(mode="json", by_alias=True, exclude_defaults=True)}
