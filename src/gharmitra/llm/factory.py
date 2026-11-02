"""Adapter registry: built-in names and `module:Class` import paths; pacing and fallback to rules."""

from __future__ import annotations

import importlib
import logging

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
