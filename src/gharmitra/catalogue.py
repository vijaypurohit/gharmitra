"""City catalogue: services, areas, aliases, currency and timezone, loaded from cities/<city>.json. Used to normalise intents and seed the mock."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass(frozen=True)
class Catalogue:
    city: str
    currency: str
    locale: str
    timezone: str
    services: dict[str, dict]
    areas: dict[str, list[str]]

    def service_label(self, service_id: str) -> str:
        return self.services.get(service_id, {}).get("label", service_id)

    def service(self, text: str | None) -> str | None:
        """Exact id, label or alias → service id."""
        if not text:
            return None
        t = _norm(text).replace(" ", "_")
        if t in self.services:
            return t
        t = _norm(text)
        for sid, info in self.services.items():
            if t == _norm(info["label"]) or t in (_norm(a) for a in info["aliases"]):
                return sid
        return None

    def area(self, text: str | None) -> str | None:
        """Exact name or alias → canonical area name."""
        if not text:
            return None
        t = _norm(text)
        for name, aliases in self.areas.items():
            if t == _norm(name) or t in (_norm(a) for a in aliases):
                return name
        return None

    def find_service(self, text: str) -> str | None:
        """First service whose alias appears as whole words in free text (longest alias wins)."""
        return self._find(text, {sid: info["aliases"] for sid, info in self.services.items()})

    def find_area(self, text: str) -> str | None:
        return self._find(text, {name: [name, *aliases] for name, aliases in self.areas.items()})

    @staticmethod
    def _find(text: str, table: dict[str, list[str]]) -> str | None:
        padded = f" {_norm(text)} "
        candidates = [(len(a), key) for key, aliases in table.items() for a in aliases if f" {_norm(a)} " in padded]
        return max(candidates)[1] if candidates else None


@lru_cache
def load_catalogue(city: str = "bengaluru") -> Catalogue:
    raw = json.loads(resources.files("gharmitra.cities").joinpath(f"{city}.json").read_text())
    return Catalogue(
        city=raw["city"],
        currency=raw["currency"],
        locale=raw["locale"],
        timezone=raw["timezone"],
        services=raw["services"],
        areas=raw["areas"],
    )
