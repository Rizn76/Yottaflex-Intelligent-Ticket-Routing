"""Repeat a configured generation and compare counts plus SHA-256 checksums."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from config import load_config
from src.generator import generate


def verify_reproducibility(
    *,
    seed: int | None = None,
    tickets: int = 100,
    agents: int = 10,
    messages: int = 250,
    event_days: int = 7,
    start_date: str | None = None,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    config = load_config()
    seed = config.seed if seed is None else seed
    start_date = config.start_date if start_date is None else start_date
    timezone_name = config.timezone_name if timezone_name is None else timezone_name
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix=".yottaflex-repro-", dir=project_root) as directory:
        first_path = Path(directory) / "first"
        second_path = Path(directory) / "second"
        options = {
            "seed": seed,
            "tickets": tickets,
            "agents": agents,
            "messages": messages,
            "event_days": event_days,
            "start_date": start_date,
            "timezone_name": timezone_name,
        }
        first = generate(**options, output_dir=str(first_path))
        second = generate(**options, output_dir=str(second_path))
        counts_match = first["counts"] == second["counts"]
        checksums_match = first["checksums_sha256"] == second["checksums_sha256"]
        result = {
            "passed": counts_match and checksums_match,
            "counts_match": counts_match,
            "checksums_match": checksums_match,
            "seed": seed,
            "configuration": options,
            "counts": first["counts"],
            "file_count": len(first["checksums_sha256"]),
        }
        if not result["passed"]:
            raise RuntimeError(f"Reproducibility verification failed: {json.dumps(result, indent=2)}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    config = load_config()
    parser.add_argument("--seed", type=int, default=config.seed)
    parser.add_argument("--tickets", type=int, default=100)
    parser.add_argument("--agents", type=int, default=10)
    parser.add_argument("--messages", type=int, default=250)
    parser.add_argument("--event-days", type=int, default=7)
    parser.add_argument("--start-date", default=config.start_date)
    parser.add_argument("--timezone", default=config.timezone_name)
    args = parser.parse_args()
    verify_reproducibility(
        seed=args.seed,
        tickets=args.tickets,
        agents=args.agents,
        messages=args.messages,
        event_days=args.event_days,
        start_date=args.start_date,
        timezone_name=args.timezone,
    )
