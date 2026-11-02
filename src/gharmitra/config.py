"""Settings: code defaults < env/.env < values saved from the Settings page (DB). Secrets are env-only, never stored, logged or returned."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gharmitra.db import Database, jsonb


@dataclass(frozen=True)
class Limits:
    model_calls_per_turn: int = 8
    tool_calls_per_turn: int = 15
    seconds_per_turn: float = 60.0
    turns_per_session: int = 40
    retries_per_operation: int = 3
    min_confidence: float = 0.6


@dataclass(frozen=True)
class Settings:
    database_url: str | None = None
    test_database_url: str | None = None
    data_dir: Path = Path("data")
    traces_dir: Path = Path("traces")
    model_adapter: str = "rules"
    model_base_url: str | None = None
    model_name: str | None = None
    model_api_key: str | None = field(default=None, repr=False)
    model_timeout_s: float = 30.0
    model_min_interval_s: float = 0.0
    trace_sinks: tuple[str, ...] = ("postgres", "jsonl")
    city: str = "bengaluru"
    currency: str = "INR"
    timezone: str = "Asia/Kolkata"
    seed: int = 42
    scenario: str = "default"
    trace_retention_days: int = 30
    limits: Limits = Limits()
    sources: dict[str, str] = field(default_factory=dict, compare=False, repr=False)  # key -> default|env|settings

    def secret_values(self) -> list[str]:
        """Values that must never appear in a trace, log or API response."""
        secrets = [self.model_api_key or ""]
        for url in (self.database_url, self.test_database_url):
            if url and (password := urlsplit(url).password):
                secrets.append(password)
        return [s for s in secrets if len(s) >= 4]


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip("'\"")
        values[key.strip()] = value
    return values


def load_settings(env: dict[str, str] | None = None, dotenv: Path | None = Path(".env")) -> Settings:
    merged = read_dotenv(dotenv) if dotenv else {}
    merged.update(os.environ if env is None else env)

    def get(key: str) -> str | None:
        value = merged.get(key, "").strip()
        return value or None

    defaults = Settings()
    sinks = get("TRACE_SINKS")
    sources = {k: ("env" if get(ENV_NAMES.get(k, "")) else "default") for k in EDITABLE}
    return Settings(
        sources=sources,
        trace_retention_days=int(get("TRACE_RETENTION_DAYS") or defaults.trace_retention_days),
        database_url=get("DATABASE_URL"),
        test_database_url=get("TEST_DATABASE_URL"),
        data_dir=Path(get("DATA_DIR") or defaults.data_dir),
        traces_dir=Path(get("TRACES_DIR") or defaults.traces_dir),
        model_adapter=get("MODEL_ADAPTER") or defaults.model_adapter,
        model_base_url=get("MODEL_BASE_URL"),
        model_name=get("MODEL_NAME"),
        model_api_key=get("MODEL_API_KEY"),
        model_timeout_s=float(get("MODEL_TIMEOUT_S") or defaults.model_timeout_s),
        model_min_interval_s=float(get("MODEL_MIN_INTERVAL_S") or defaults.model_min_interval_s),
        trace_sinks=tuple(s.strip() for s in sinks.split(",") if s.strip()) if sinks else defaults.trace_sinks,
        city=get("CITY") or defaults.city,
        currency=get("CURRENCY") or defaults.currency,
        timezone=get("TIMEZONE") or defaults.timezone,
        seed=int(get("SEED") or defaults.seed),
        scenario=get("SCENARIO") or defaults.scenario,
    )


# --- Settings page layer (stored in the agent `settings` table) -------------------------------------

STORE_KEY = "app_settings"
# What the Settings page may change. Secrets and connection details stay env-only.
EDITABLE: dict[str, type] = {
    "model_adapter": str, "model_base_url": str, "model_name": str,
    "model_timeout_s": float, "model_min_interval_s": float, "trace_retention_days": int,
    **{f"limits.{f.name}": {"int": int, "float": float}[str(f.type)] for f in fields(Limits)},
}
ENV_NAMES = {"model_adapter": "MODEL_ADAPTER", "model_base_url": "MODEL_BASE_URL", "model_name": "MODEL_NAME",
             "model_timeout_s": "MODEL_TIMEOUT_S", "model_min_interval_s": "MODEL_MIN_INTERVAL_S",
             "trace_retention_days": "TRACE_RETENTION_DAYS"}


def _coerce(key: str, value: Any) -> Any:
    kind = EDITABLE.get(key)
    if kind is None:
        raise ValueError(f"{key} can't be changed in Settings")
    if value is None or value == "":
        return None
    try:
        out = kind(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: expected {kind.__name__}") from None
    if kind in (int, float) and out < 0:
        raise ValueError(f"{key} must be zero or more")
    return out


def editable_values(s: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in EDITABLE:
        if key.startswith("limits."):
            out[key] = getattr(s.limits, key.split(".", 1)[1])
        else:
            out[key] = getattr(s, key)
    return out


def apply_overrides(base: Settings, overrides: dict[str, Any]) -> Settings:
    """Saved values over env over defaults. Unknown or invalid saved keys are ignored."""
    top: dict[str, Any] = {}
    limits: dict[str, Any] = {}
    sources = dict(base.sources) or {k: "default" for k in EDITABLE}
    for key, raw in overrides.items():
        try:
            value = _coerce(key, raw)
        except ValueError:
            continue
        if value is None:
            continue
        if key.startswith("limits."):
            limits[key.split(".", 1)[1]] = value
        else:
            top[key] = value
        sources[key] = "settings"
    return replace(base, **top, limits=replace(base.limits, **limits), sources=sources)


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self) -> dict[str, Any]:
        return self.db.scalar("select value from settings where key = %s", (STORE_KEY,)) or {}

    def update(self, changes: dict[str, Any], now: Any) -> dict[str, Any]:
        """Validate all changes first; None or "" removes a saved value (back to env or default)."""
        current = self.get()
        for key, raw in changes.items():
            value = _coerce(key, raw)
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        self.db.execute(
            "insert into settings (key, value, updated_at) values (%s, %s, %s)"
            " on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at",
            (STORE_KEY, jsonb(current), now),
        )
        return current
