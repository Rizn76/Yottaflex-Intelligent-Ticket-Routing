"""Project configuration with optional YottaFlex environment overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Mapping
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    seed: int = 20261022
    tickets: int = 30_000
    agents: int = 100
    messages: int = 100_000
    event_days: int = 90
    start_date: str = "2026-08-01"
    output_dir: str = "data/generated"
    timezone_name: str = "Asia/Kolkata"

    def __post_init__(self) -> None:
        date.fromisoformat(self.start_date)
        ZoneInfo(self.timezone_name)


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Load defaults, overridden by YOTTAFLEX_* environment variables."""
    values = os.environ if environ is None else environ
    defaults = Config()

    def integer(name: str, default: int) -> int:
        value = values.get(f"YOTTAFLEX_{name.upper()}")
        return default if value is None else int(value)

    output_dir = values.get(
        "YOTTAFLEX_OUTPUT_DIR",
        values.get("YOTTAFLEX_DATA_DIR", defaults.output_dir),
    )
    return Config(
        seed=integer("seed", defaults.seed),
        tickets=integer("tickets", defaults.tickets),
        agents=integer("agents", defaults.agents),
        messages=integer("messages", defaults.messages),
        event_days=integer("event_days", defaults.event_days),
        start_date=values.get("YOTTAFLEX_START_DATE", defaults.start_date),
        output_dir=output_dir,
        timezone_name=values.get("YOTTAFLEX_TIMEZONE", defaults.timezone_name),
    )


CONFIG = load_config()
