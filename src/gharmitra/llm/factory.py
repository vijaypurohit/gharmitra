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


def make_model(settings: Settings) -> ModelClient:
    name = settings.model_adapter
    if name == "rules":
        return RuleModel()
    if ":" in name:
        module, _, cls = name.partition(":")
        return getattr(importlib.import_module(module), cls)(settings)
    builtin = {"openai": "gharmitra.llm.openai_api:OpenAIModel",
               "anthropic": "gharmitra.llm.anthropic_api:AnthropicModel",
               "claude_cli": "gharmitra.llm.claude_cli:ClaudeCliModel"}
    if name not in builtin:
        raise ValueError(f"unknown MODEL_ADAPTER {name!r}")
    module, _, cls = builtin[name].partition(":")
    try:
        return getattr(importlib.import_module(module), cls)(settings)
    except AttributeError:
        log.warning("model adapter %s is not built yet; using rules", name)
        return RuleModel()


def probe(settings: Settings, text: str, today: date | None = None) -> dict:
    """Parse one message with the configured model: intent, latency, tokens, cost (or the error)."""
    from gharmitra.catalogue import load_catalogue
    from gharmitra.llm.base import Intent, ModelError
    from gharmitra.llm.prompt import build_prompt

    model = make_model(settings)
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
