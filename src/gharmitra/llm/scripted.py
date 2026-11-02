"""ScriptedModel: canned intents per turn, for evals.

Maps each scripted user message to a reply: an Intent dict, a raw string (to test bad output) or a
ModelError (to test fallback). It records every prompt it receives, so evals can check what a model
would have been shown. A message with no script entry is a test bug, so it raises instead of
falling back.
"""

from __future__ import annotations

import json
from typing import Any

from gharmitra.llm.base import ModelError, ModelResult, Prompt, Usage


class ScriptMiss(LookupError):
    pass


class ScriptedModel:
    adapter = "scripted"
    model = "scripted"

    def __init__(self, script: dict[str, Any] | None = None) -> None:
        self.script: dict[str, Any] = dict(script or {})
        self.prompts: list[Prompt] = []

    def add(self, text: str, reply: Any) -> None:
        self.script[text] = reply

    def complete(self, prompt: Prompt) -> ModelResult:
        self.prompts.append(prompt)
        if prompt.user not in self.script:
            raise ScriptMiss(f"no scripted reply for {prompt.user!r}")
        reply = self.script[prompt.user]
        if isinstance(reply, ModelError):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return ModelResult(text=text, usage=Usage(input_tokens=len(prompt.system) // 4, output_tokens=len(text) // 4))
