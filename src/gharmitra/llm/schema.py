"""The Intent JSON schema in the subset structured-output APIs accept.

Numeric and length constraints, defaults and titles are dropped (pydantic still checks them on the
reply), and every object gets `additionalProperties: false`. OpenAI's strict mode also needs every
property listed as required; optional fields stay nullable through their `anyOf ... null`.
"""

from __future__ import annotations

from typing import Any

from gharmitra.llm.base import intent_schema

DROP = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
        "default", "title"}


def strict_schema(require_all: bool = False) -> dict[str, Any]:
    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(n) for n in node]
        if not isinstance(node, dict):
            return node
        out = {k: clean(v) for k, v in node.items() if k not in DROP or k == "properties"}
        if "properties" in node:  # property names may collide with DROP words; keep them all
            out["properties"] = {k: clean(v) for k, v in node["properties"].items()}
        if out.get("type") == "object":
            out["additionalProperties"] = False
            if require_all:
                out["required"] = list(out.get("properties", {}))
        return out

    return clean(intent_schema())


def strip_fences(text: str) -> str:
    """Some OpenAI-compatible servers wrap JSON in ``` fences even in JSON mode."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()
