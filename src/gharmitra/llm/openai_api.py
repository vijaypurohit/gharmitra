"""openai adapter: the OpenAI chat-completions protocol over plain HTTP.

One adapter serves OpenAI, Gemini, Ollama, LM Studio, OpenRouter, Groq and vLLM; MODEL_BASE_URL decides
who answers. It asks for a strict JSON schema; if the server rejects `json_schema`, it drops to
`json_object` once (the schema is in the system prompt either way) and stays there.
"""

from __future__ import annotations

import httpx

from gharmitra.config import Settings
from gharmitra.llm.base import ModelError, ModelResult, Prompt, Usage
from gharmitra.llm.schema import strict_schema, strip_fences

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIModel:
    adapter = "openai"

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.model_name:
            raise ValueError("MODEL_NAME is required for the openai adapter")
        self.model = settings.model_name
        headers = {"Authorization": f"Bearer {settings.model_api_key}"} if settings.model_api_key else {}
        base = (settings.model_base_url or DEFAULT_BASE_URL).rstrip("/") + "/"
        self.http = httpx.Client(base_url=base, headers=headers, timeout=settings.model_timeout_s, transport=transport)
        self.json_mode = "json_schema"
        self.schema = strict_schema(require_all=True)

    def _body(self, prompt: Prompt) -> dict:
        if self.json_mode == "json_schema":
            response_format = {"type": "json_schema",
                               "json_schema": {"name": "intent", "strict": True, "schema": self.schema}}
        else:
            response_format = {"type": "json_object"}
        return {"model": self.model, "response_format": response_format,
                "messages": [{"role": "system", "content": prompt.system}, {"role": "user", "content": prompt.user}]}

    def complete(self, prompt: Prompt) -> ModelResult:
        r = self._post(prompt)
        if r.status_code == 400 and self.json_mode == "json_schema" and any(
                w in r.text.lower() for w in ("response_format", "json_schema", "schema")):
            self.json_mode = "json_object"
            r = self._post(prompt)
        if r.status_code == 429:
            raise ModelError("rate_limited", f"429 {r.text[:200]}")
        if r.status_code >= 400:
            raise ModelError("http_error", f"{r.status_code} {r.text[:200]}")
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("bad_output", f"unexpected response: {exc}") from None
        u = data.get("usage") or {}
        return ModelResult(strip_fences(text), Usage(u.get("prompt_tokens"), u.get("completion_tokens"), None))

    def _post(self, prompt: Prompt) -> httpx.Response:
        try:
            return self.http.post("chat/completions", json=self._body(prompt))
        except httpx.TimeoutException:
            raise ModelError("timeout", "request timed out") from None
        except httpx.HTTPError as exc:
            raise ModelError("http_error", type(exc).__name__) from None
