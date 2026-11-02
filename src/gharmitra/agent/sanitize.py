"""Treat tool free-text as data: sanitise, truncate, flag injection attempts.

Detection is a second line of defence. The guarantee is structural: tool text never reaches the
model, tools are chosen only by the controller, and approval comes only from user turns.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

HIDDEN_REVIEW = "[review hidden: suspicious content]"
MAX_REVIEW = 240
MAX_NAME = 60
MAX_ATTRIBUTE = 40

INJECTION = re.compile(
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|messages)"
    r"|disregard (your|all|the|previous)"
    r"|\b(system|assistant|developer)\s*:"
    r"|system override|you are now|note to (the )?(ai|agent|assistant)"
    r"|\bapprov(e|ed|al)\b|already approved|book (it |this )?now|confirm the booking"
    r"|skip (the )?confirmation|without asking|cancel the user",
    re.I,
)
_MARKUP = re.compile(r"<[^>]{0,200}>|&[a-z]+;|[`*_#]{2,}")


def clean(text: Any, limit: int = MAX_REVIEW) -> str:
    """Strip control and format characters and markup, collapse whitespace, truncate."""
    s = "".join(ch for ch in str(text) if unicodedata.category(ch)[0] != "C" or ch in "\n\t")
    s = _MARKUP.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def is_suspicious(text: str) -> bool:
    return bool(INJECTION.search(text))


class Sanitizer:
    """Cleans tool results before anything else sees them. `flags` collects what was flagged this call."""

    def __init__(self) -> None:
        self.flags: list[dict[str, str]] = []

    def _flag(self, field: str, ref: str) -> None:
        self.flags.append({"field": field, "ref": ref})

    def name(self, value: str, ref: str) -> str:
        raw = clean(value, 200)
        if not is_suspicious(raw):
            return clean(raw, MAX_NAME)
        self._flag("name", ref)
        head = re.split(r"[\[\(<{|]|\b(system|assistant|ignore|note)\b", raw, maxsplit=1, flags=re.I)[0].strip(" -:,")
        return clean(head, MAX_NAME) or f"Provider {ref}"

    def attributes(self, values: list[Any], ref: str) -> list[str]:
        out = []
        for v in values or []:
            text = clean(v, 200)
            if is_suspicious(text):
                self._flag("attribute", ref)
                continue
            out.append(clean(text, MAX_ATTRIBUTE))
        return out

    def review(self, text: str, ref: str) -> str:
        cleaned = clean(text, 1000)
        if is_suspicious(cleaned):
            self._flag("review", ref)
            return HIDDEN_REVIEW
        return clean(cleaned, MAX_REVIEW)

    def error(self, text: str) -> str:
        cleaned = clean(text, 160)
        if is_suspicious(cleaned):
            self._flag("error", "")
            return "[error text hidden: suspicious content]"
        return cleaned

    def result(self, tool: str, result: Any) -> Any:
        """Sanitise one tool result by its known shape."""
        if tool == "search_providers":
            return [self.provider(p) for p in result]
        if tool == "get_provider":
            p = self.provider(result)
            p["reviews"] = [
                {"author": clean(r.get("author", ""), 40), "rating": r.get("rating"),
                 "text": self.review(r.get("text", ""), result.get("id", ""))}
                for r in result.get("reviews", [])
            ]
            return p
        if tool in ("list_bookings",):
            return [{**b, "provider_name": self.name(b.get("provider_name", ""), b.get("provider_id", ""))} for b in result]
        if tool == "reschedule" and isinstance(result, dict) and "provider_name" in result:
            return {**result, "provider_name": self.name(result["provider_name"], result.get("provider_id", ""))}
        return result

    def provider(self, p: dict) -> dict:
        out = dict(p)
        ref = p.get("id", "")
        if "name" in p:
            out["name"] = self.name(p["name"], ref)
        if "attributes" in p:
            out["attributes"] = self.attributes(p["attributes"], ref)
        return out
