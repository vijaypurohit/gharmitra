"""anthropic adapter: the Claude Messages API through the official SDK, with structured JSON output.

`output_config.format` constrains the reply to the Intent schema. Thinking can't be turned off on
current Claude models, so the adapter asks for `low` effort: intent parsing is a small extraction.
"""

from __future__ import annotations

from typing import Any

import anthropic

from gharmitra.config import Settings
from gharmitra.llm.base import ModelError, ModelResult, Prompt, Usage
from gharmitra.llm.schema import strict_schema

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 4096  # thinking counts against this; the intent itself is ~100 tokens
# USD per million tokens (input, output), for the run report. Unknown models report no cost.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
EFFORT_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-fable-5")


class AnthropicModel:
    adapter = "anthropic"

    def __init__(self, settings: Settings, http_client: Any = None) -> None:
        self.model = settings.model_name or DEFAULT_MODEL
        self.client = anthropic.Anthropic(
            api_key=settings.model_api_key,  # None: the SDK resolves ANTHROPIC_API_KEY or a login profile
            base_url=settings.model_base_url or None,
            timeout=settings.model_timeout_s,
            max_retries=1,  # a failed call falls back to the rules parser; don't stall the turn
            http_client=http_client,
        )
        self.output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": strict_schema()}}
        if self.model.startswith(EFFORT_MODELS):
            self.output_config["effort"] = "low"

    def complete(self, prompt: Prompt) -> ModelResult:
        try:
            r = self.client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=prompt.system,
                messages=[{"role": "user", "content": prompt.user}], output_config=self.output_config,
            )
        except anthropic.APITimeoutError:
            raise ModelError("timeout", "request timed out") from None
        except anthropic.RateLimitError as exc:
            raise ModelError("rate_limited", f"429 {exc.message[:200]}") from None
        except anthropic.APIStatusError as exc:
            raise ModelError("http_error", f"{exc.status_code} {exc.message[:200]}") from None
        except anthropic.APIConnectionError:
            raise ModelError("http_error", "connection error") from None
        if r.stop_reason in ("refusal", "max_tokens"):
            raise ModelError("bad_output", f"stop_reason {r.stop_reason}")
        text = next((b.text for b in r.content if b.type == "text"), "")
        usage = Usage(r.usage.input_tokens, r.usage.output_tokens, self._cost(r.usage.input_tokens, r.usage.output_tokens))
        return ModelResult(text, usage)

    def _cost(self, inp: int | None, out: int | None) -> float | None:
        price = PRICES.get(self.model)
        if price is None or inp is None or out is None:
            return None
        return round((inp * price[0] + out * price[1]) / 1_000_000, 6)
