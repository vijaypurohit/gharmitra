"""Settings: code defaults < env/.env < values saved from the Settings page (DB). Secrets are env-only, never stored, logged or returned."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


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
    limits: Limits = Limits()

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
    return Settings(
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
