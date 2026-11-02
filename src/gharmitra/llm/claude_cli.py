"""claude_cli adapter: runs the local `claude` CLI with JSON-schema output, using its own login.

No API key or credits needed. The CLI runs as an argument list (no shell) in an empty temp directory
with no tools, no saved session, no settings files and no MCP servers, so it can only answer. The
message goes in on stdin, never as an argument.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile

from gharmitra.config import Settings
from gharmitra.llm.base import ModelError, ModelResult, Prompt, Usage
from gharmitra.llm.schema import strict_schema

DEFAULT_MODEL = "sonnet"


class ClaudeCliModel:
    adapter = "claude_cli"

    def __init__(self, settings: Settings, command: str | None = None) -> None:
        self.model = settings.model_name or DEFAULT_MODEL
        self.command = command or shutil.which("claude")
        self.timeout = settings.model_timeout_s
        self.schema = json.dumps(strict_schema(), separators=(",", ":"))

    def args(self, prompt: Prompt) -> list[str]:
        return [self.command or "claude", "-p", "--output-format", "json", "--json-schema", self.schema,
                "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
                "--model", self.model, "--system-prompt", prompt.system]

    def complete(self, prompt: Prompt) -> ModelResult:
        if not self.command:
            raise ModelError("process_error", "the claude CLI is not on PATH")
        with tempfile.TemporaryDirectory(prefix="gm-claude-") as cwd:
            try:
                p = subprocess.run(self.args(prompt), input=prompt.user, capture_output=True, text=True,
                                   timeout=self.timeout, cwd=cwd, check=False)
            except subprocess.TimeoutExpired:
                raise ModelError("timeout", f"claude CLI took over {self.timeout:.0f}s") from None
            except OSError as exc:
                raise ModelError("process_error", str(exc)) from None
        try:
            out = json.loads(p.stdout)
        except ValueError:
            out = None
        if p.returncode != 0 or not isinstance(out, dict) or out.get("is_error"):
            detail = (out or {}).get("result") if isinstance(out, dict) else (p.stderr or p.stdout)
            detail = str(detail or f"exit {p.returncode}")[:200]
            kind = "rate_limited" if "limit" in detail.lower() else "process_error"
            raise ModelError(kind, detail)
        structured = out.get("structured_output")
        text = json.dumps(structured) if structured is not None else str(out.get("result") or "")
        u = out.get("usage") or {}
        prompt_tokens = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                    "cache_creation_input_tokens")) if u else None
        return ModelResult(text, Usage(prompt_tokens, u.get("output_tokens"), out.get("total_cost_usd")))
