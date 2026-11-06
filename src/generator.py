"""Reproducible synthetic data generator for the YottaFlex ticket-routing challenge."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import random
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import load_config
from src.database import SCHEMA_VERSION, initialize_database
from src.routing import route_ticket
from src.sla_engine import add_business_minutes, breach_minutes, business_minutes_between, calculate_sla


CHANNELS = ["email", "chat", "phone"]
LANGUAGES = ["English", "Hindi", "Telugu", "Tamil", "Kannada", "Spanish", "French"]
INTENTS = ["billing", "technical_support", "account_access", "sales", "refund", "delivery", "complaint"]
PRIORITIES = ["low", "normal", "high", "critical"]
SKILLS = INTENTS.copy()
PRIORITY_RANK = {"low": 1, "normal": 2, "high": 3, "critical": 4}
BASE_SLA_MINUTES = {
    "critical": (5, 60),
    "high": (15, 240),
    "normal": (60, 1440),
    "low": (240, 4320),
}
CHANNEL_MULTIPLIER = {"email": 1.0, "chat": 0.75, "phone": 0.5}
ANOMALY_TYPES = [
    "agent_over_capacity",
    "sla_ignores_channel_calendar",
    "reopen_loses_history",
    "transfer_loop",
    "priority_safety_contradiction",
]
ANOMALY_DATABASE_COLUMNS = (
    "anomaly_id",
    "ticket_id",
    "anomaly_type",
    "description",
    "expected_validation",
    "ground_truth_label",
)
ANOMALY_GROUND_TRUTH_COLUMNS = (
    *ANOMALY_DATABASE_COLUMNS,
    "affected_entity_ids",
    "event_references",
    "reference_timestamp",
    "expected_behavior",
    "observed_behavior",
    "severity",
    "seed",
)


def deterministic_id(prefix: str, number: int, width: int = 6) -> str:
    return f"{prefix}-{number:0{width}d}"


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _calendar_args(calendar: dict[str, Any]) -> dict[str, Any]:
    return {
        "timezone_name": calendar["timezone"],
        "business_start": time.fromisoformat(calendar["business_hours_start"]),
        "business_end": time.fromisoformat(calendar["business_hours_end"]),
        "weekdays": frozenset(calendar["weekdays"]),
        "holidays": frozenset(date.fromisoformat(day) for day in calendar["holidays"]),
    }


def _write_database(path: Path, tables: dict[str, pd.DataFrame]) -> None:
    initialize_database(path)
    insertion_order = [
        "business_calendars", "calendar_business_days", "calendar_holidays",
        "languages", "channels", "intents", "priority_rules", "customers",
        "agents", "skills", "agent_skills", "agent_languages", "queues",
        "tickets", "messages", "assignment_events", "sla_events", "ticket_events",
        "resolutions", "anomalies", "ground_truth_labels",
    ]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for table_name in insertion_order:
            frame = tables[table_name]
            if frame.empty:
                continue
            columns = (
                list(ANOMALY_DATABASE_COLUMNS)
                if table_name == "anomalies"
                else list(frame.columns)
            )
            placeholders = ",".join("?" for _ in columns)
            quoted_columns = ",".join(f'"{column}"' for column in columns)
            rows = [
                tuple(None if pd.isna(value) else value for value in row)
                for row in frame[columns].itertuples(index=False, name=None)
            ]
            connection.executemany(
                f'INSERT INTO "{table_name}" ({quoted_columns}) VALUES ({placeholders})',
                rows,
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"Generated database contains foreign-key violations: {violations[:5]}")


def _make_calendars(
    start: datetime,
    event_days: int,
    timezone_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    holiday_dates = sorted({
        (start.date() + timedelta(days=min(event_days - 1, event_days // 3))).isoformat(),
        (start.date() + timedelta(days=min(event_days - 1, (2 * event_days) // 3))).isoformat(),
    })
    specifications = {
        "email": ("09:00", "18:00", [0, 1, 2, 3, 4]),
        "chat": ("08:00", "20:00", [0, 1, 2, 3, 4, 5]),
        "phone": ("00:00", "00:00", [0, 1, 2, 3, 4, 5, 6]),
    }
    calendars, business_days, holidays, by_channel = [], [], [], {}
    for channel, (opening, closing, weekdays) in specifications.items():
        calendar_id = f"CAL-{channel.upper()}"
        calendar_holidays = [] if channel == "phone" else holiday_dates
        calendar = {
            "calendar_id": calendar_id,
            "calendar_name": f"{channel.title()} support calendar",
            "timezone": timezone_name,
            "business_hours_start": opening,
            "business_hours_end": closing,
            "weekdays": weekdays,
            "holidays": calendar_holidays,
        }
        calendars.append({key: value for key, value in calendar.items() if key not in {"weekdays", "holidays"}})
        business_days.extend({"calendar_id": calendar_id, "weekday": day} for day in weekdays)
        holidays.extend(
            {"calendar_id": calendar_id, "holiday_date": holiday, "holiday_name": "Synthetic service holiday"}
            for holiday in calendar_holidays
        )
        by_channel[channel] = calendar
    return calendars, business_days, holidays, by_channel


def _target_minutes(priority: str, channel: str) -> tuple[int, int]:
    response, resolution = BASE_SLA_MINUTES[priority]
    multiplier = CHANNEL_MULTIPLIER[channel]
    return max(1, math.ceil(response * multiplier)), max(1, math.ceil(resolution * multiplier))


def _event(
    events: list[dict[str, Any]],
    ticket_id: str,
    event_type: str,
    event_at: datetime,
    from_state: str | None,
    to_state: str | None,
    details: str,
    actor_id: str | None = None,
) -> None:
    events.append({
        "event_id": f"EVT-{len(events) + 1:08d}",
        "ticket_id": ticket_id,
        "event_type": event_type,
        "from_state": from_state,
        "to_state": to_state,
        "actor_id": actor_id,
        "event_at": _iso(event_at),
        "details": details,
        "anomaly_type": None,
    })


def _with_anomalies(
    tickets: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    sla_events: list[dict[str, Any]],
    events: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    seed: int,
    channels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Inject five isolated, labeled edge cases after the valid baseline is built."""
    by_ticket = {ticket["ticket_id"]: ticket for ticket in tickets}
    truth_by_ticket = {row["ticket_id"]: row for row in ground_truth}
    anomalies: list[dict[str, Any]] = []
    selected_ids = [f"TKT-2026-{index:06d}" for index in range(1, 6)]

    def label(
        index: int,
        anomaly_type: str,
        description: str,
        expected: str,
        affected_entities: dict[str, Any],
        reference_timestamp: datetime,
        expected_behavior: str,
        observed_behavior: str,
        severity: str,
        *,
        ticket_event_types: list[str] | None = None,
        assignment_ids: list[str] | None = None,
        sla_ids: list[str] | None = None,
    ) -> None:
        ticket_id = selected_ids[index]
        anomaly_id = f"ANM-{seed}-{index + 1:02d}"
        anomalies.append({
            "anomaly_id": anomaly_id,
            "ticket_id": ticket_id,
            "anomaly_type": anomaly_type,
            "description": description,
            "expected_validation": expected,
            "ground_truth_label": "expected_detection",
            "affected_entity_ids": json.dumps(affected_entities, sort_keys=True),
            "event_references": "",
            "reference_timestamp": _iso(reference_timestamp),
            "expected_behavior": expected_behavior,
            "observed_behavior": observed_behavior,
            "severity": severity,
            "seed": seed,
            "_ticket_event_types": ticket_event_types or [],
            "_assignment_ids": assignment_ids or [],
            "_sla_ids": sla_ids or [],
        })
        truth_by_ticket[ticket_id]["anomaly_type"] = anomaly_type
        truth_by_ticket[ticket_id]["expected_validation"] = expected

    # Two concurrent records on the dedicated one-slot agent exceed its capacity.
    base_assignment = next(
        row for row in assignments
        if row["ticket_id"] == selected_ids[0] and row["agent_id"] is not None
    )
    capacity_agent = next(agent for agent in agents if agent["agent_id"] == "AGT-000001")
    overlap_start = _parse_datetime(base_assignment["assigned_at"]) + timedelta(seconds=5)
    overlap_end = overlap_start + timedelta(seconds=10)
    base_assignment["unassigned_at"] = _iso(overlap_start)
    base_assignment["assignment_status"] = "transferred"
    for number in (2, 3):
        assignments.append({
            "assignment_id": f"ASN-{selected_ids[0]}-{number:03d}",
            "ticket_id": selected_ids[0],
            "agent_id": capacity_agent["agent_id"],
            "queue_id": base_assignment["queue_id"],
            "assigned_at": _iso(overlap_start),
            "unassigned_at": _iso(overlap_end),
            "assignment_status": "transferred" if number == 2 else "assigned",
            "decision_reason": "injected anomaly: assignment exceeds agent concurrency",
            "assignment_number": number,
        })
    _event(events, selected_ids[0], "capacity_violation", overlap_start, "assigned", "assigned",
           f"Injected overlapping assignments to one-slot agent {capacity_agent['agent_id']}",
           capacity_agent["agent_id"])
    label(
        0,
        ANOMALY_TYPES[0],
        "Two simultaneous assignments exceed the dedicated agent's one-ticket limit.",
        "CAPACITY_VIOLATION",
        {
            "agent_ids": [capacity_agent["agent_id"]],
            "assignment_ids": [f"ASN-{selected_ids[0]}-002", f"ASN-{selected_ids[0]}-003"],
        },
        overlap_start,
        "The agent must have no more than one overlapping assignment.",
        "Two assignment records overlap on the one-slot agent.",
        "high",
        ticket_event_types=["capacity_violation"],
        assignment_ids=[
            f"ASN-{selected_ids[0]}-001",
            f"ASN-{selected_ids[0]}-002",
            f"ASN-{selected_ids[0]}-003",
        ],
    )

    # Preserve an elapsed-time deadline instead of the channel calendar deadline.
    ticket_id = selected_ids[1]
    ticket = by_ticket[ticket_id]
    target = next(row for row in sla_events if row["ticket_id"] == ticket_id and row["sla_type"] == "resolution")
    target["due_at"] = _iso(_parse_datetime(target["started_at"]) + timedelta(minutes=target["target_minutes"]))
    channel = next(
        row for row in channels if row["channel_id"] == ticket["channel_id"]
    )
    label(
        1,
        ANOMALY_TYPES[1],
        "Resolution deadline uses elapsed minutes instead of the channel's business calendar.",
        "SLA_DEADLINE_MISMATCH",
        {
            "channel_id": ticket["channel_id"],
            "calendar_id": channel["calendar_id"],
            "sla_id": target["sla_id"],
        },
        _parse_datetime(target["started_at"]),
        "The deadline must be calculated using the channel's timezone, hours, weekdays, and holidays.",
        f"Deadline was set to elapsed target minutes: {target['due_at']}.",
        "high",
        sla_ids=[str(target["sla_id"])],
    )

    # Add a transfer cycle while retaining every baseline assignment record.
    ticket_id = selected_ids[2]
    ticket = by_ticket[ticket_id]
    history = sorted(
        [row for row in assignments if row["ticket_id"] == ticket_id],
        key=lambda row: row["assignment_number"],
    )
    first = history[0]
    original_agent = first["agent_id"] or agents[min(1, len(agents) - 1)]["agent_id"]
    first["agent_id"] = original_agent
    first["assignment_status"] = "transferred"
    preferred_agents = [agent["agent_id"] for agent in agents[1:3]]
    alternate_agent = next(agent_id for agent_id in preferred_agents if agent_id != original_agent)
    first_time = _parse_datetime(first["assigned_at"])
    final_release = (
        _parse_datetime(first["unassigned_at"])
        if first["unassigned_at"] else first_time + timedelta(minutes=15)
    )
    transfer_one = first_time + timedelta(seconds=10)
    transfer_two = first_time + timedelta(seconds=20)
    first["unassigned_at"] = _iso(transfer_one)
    first["assignment_status"] = "transferred"
    for number, agent_id, assigned_at, released_at in (
        (2, alternate_agent, transfer_one, transfer_two),
        (3, original_agent, transfer_two, max(final_release, transfer_two + timedelta(minutes=1))),
    ):
        assignments.append({
            "assignment_id": f"ASN-{ticket_id}-{number:03d}",
            "ticket_id": ticket_id,
            "agent_id": agent_id,
            "queue_id": ticket["queue_id"],
            "assigned_at": _iso(assigned_at),
            "unassigned_at": _iso(released_at),
            "assignment_status": "transferred" if number == 2 else "assigned",
            "decision_reason": "injected anomaly: transfer cycle",
            "assignment_number": number,
        })
        _event(events, ticket_id, "transferred", assigned_at, "assigned", "assigned",
               f"Injected transfer cycle to {agent_id}", agent_id)
    label(
        2,
        ANOMALY_TYPES[3],
        "Assignment history returns to an agent already visited by this ticket.",
        "TRANSFER_LOOP",
        {
            "agent_ids": [original_agent, alternate_agent],
            "queue_ids": [str(ticket["queue_id"])],
            "assignment_ids": [
                f"ASN-{ticket_id}-001",
                f"ASN-{ticket_id}-002",
                f"ASN-{ticket_id}-003",
            ],
        },
        transfer_two,
        "Transfer destinations must not return to a previously visited agent.",
        f"Transfer path visits {original_agent}, {alternate_agent}, then {original_agent}.",
        "medium",
        ticket_event_types=["transferred"],
        assignment_ids=[
            f"ASN-{ticket_id}-001",
            f"ASN-{ticket_id}-002",
            f"ASN-{ticket_id}-003",
        ],
    )

    # This isolated ticket loses only its creation event to represent missing reopen history.
    ticket_id = selected_ids[3]
    resolution = next(row for row in resolutions if row["ticket_id"] == ticket_id)
    resolved_at = _parse_datetime(resolution["resolved_at"])
    creation_event = next((row for row in events if row["ticket_id"] == ticket_id and row["event_type"] == "created"), None)
    if creation_event is not None:
        events.remove(creation_event)
    reopen_at = resolved_at + timedelta(minutes=10)
    _event(events, ticket_id, "reopened", reopen_at, "resolved", "open",
           "Reopened after prior resolution; injected missing creation history")
    by_ticket[ticket_id]["status"] = "open"
    by_ticket[ticket_id]["updated_at"] = _iso(reopen_at)
    label(
        3,
        ANOMALY_TYPES[2],
        "A reopened ticket is missing its original creation event.",
        "MISSING_LIFECYCLE_HISTORY",
        {
            "missing_event_type": "created",
            "resolution_id": str(resolution["resolution_id"]),
        },
        reopen_at,
        "Reopening must preserve the original creation event and earlier lifecycle history.",
        "The original created event is absent; the reopen event remains.",
        "medium",
        ticket_event_types=["reopened"],
    )

    # Complaint tickets must be at least high priority; the injected low value contradicts policy.
    ticket_id = selected_ids[4]
    by_ticket[ticket_id]["priority"] = "low"
    created = _parse_datetime(by_ticket[ticket_id]["created_at"])
    label(
        4,
        ANOMALY_TYPES[4],
        "Complaint safety policy requires high or critical priority, but this case is low.",
        "PRIORITY_CONTRADICTION",
        {
            "intent_id": str(by_ticket[ticket_id]["intent_id"]),
            "ticket_id": ticket_id,
        },
        created,
        "Complaint tickets must have high or critical priority.",
        "The complaint ticket priority was changed to low.",
        "high",
        ticket_event_types=["created"],
    )
    return anomalies


def generate(
    seed: int | None = None,
    tickets: int | None = None,
    agents: int | None = None,
    messages: int | None = None,
    event_days: int | None = None,
    start_date: str | None = None,
    output_dir: str | Path | None = None,
    timezone_name: str | None = None,
    inject_anomalies: bool = True,
) -> dict[str, Any]:
    config = load_config()
    seed = config.seed if seed is None else seed
    tickets = config.tickets if tickets is None else tickets
    agents = config.agents if agents is None else agents
    messages = config.messages if messages is None else messages
    event_days = config.event_days if event_days is None else event_days
    start_date = config.start_date if start_date is None else start_date
    output_dir = config.output_dir if output_dir is None else output_dir
    timezone_name = config.timezone_name if timezone_name is None else timezone_name
    ZoneInfo(timezone_name)

    if tickets < (5 if inject_anomalies else 1):
        minimum = "five" if inject_anomalies else "one"
        raise ValueError(f"At least {minimum} ticket(s) are required for this generation mode.")
    if inject_anomalies and agents < 3:
        raise ValueError("At least three agents are required for valid routing and transfer-loop scenarios.")
    if agents < 1:
        raise ValueError("At least one agent is required.")
    if messages < 0 or event_days < 1:
        raise ValueError("messages must be non-negative and event_days must be positive.")
    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    randomizer = random.Random(seed)
    rng = np.random.default_rng(seed)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for generated_report in (
        "analytics_summary.json", "validation_report.json", "validation_summary.txt",
        "events.csv", "assignments.csv", "ground_truth.csv",
    ):
        report_path = out / generated_report
        if report_path.exists():
            report_path.unlink()
    truth_dir = out / "ground_truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    json_dir = out / "json"
    json_dir.mkdir(parents=True, exist_ok=True)

    calendar_rows, calendar_day_rows, holiday_rows, calendars = _make_calendars(
        start, event_days, timezone_name
    )
    channel_rows = [{
        "channel_id": f"CH-{index:02d}",
        "channel_name": channel,
        "business_hours_start": calendar["business_hours_start"],
        "business_hours_end": calendar["business_hours_end"],
        "timezone": calendar["timezone"],
        "calendar_id": calendar["calendar_id"],
    } for index, (channel, calendar) in enumerate(calendars.items(), 1)]
    channel_by_name = {row["channel_name"]: row for row in channel_rows}
    language_rows = [
        {"language_id": f"LANG-{index:02d}", "language_name": language}
        for index, language in enumerate(LANGUAGES, 1)
    ]
    language_id = {row["language_name"]: row["language_id"] for row in language_rows}
    intent_rows = [{
        "intent_id": f"INT-{index:02d}",
        "intent_name": intent,
        "default_priority": "high" if intent == "complaint" else "normal",
    } for index, intent in enumerate(INTENTS, 1)]
    intent_id = {row["intent_name"]: row["intent_id"] for row in intent_rows}
    skill_rows = [{"skill_id": f"SKL-{index:02d}", "skill_name": skill}
                  for index, skill in enumerate(SKILLS, 1)]
    skill_id = {row["skill_name"]: row["skill_id"] for row in skill_rows}

    priority_rows = []
    for priority in PRIORITIES:
        for channel in CHANNELS:
            response_minutes, resolution_minutes = _target_minutes(priority, channel)
            priority_rows.append({
                "priority_rule_id": f"PR-{priority.upper()}-{channel.upper()}",
                "priority": priority,
                "channel_id": channel_by_name[channel]["channel_id"],
                "first_response_minutes": response_minutes,
                "resolution_minutes": resolution_minutes,
                "effective_from": _iso(start),
                "effective_to": None,
            })

    customer_count = max(100, tickets // 3)
    customer_rows = [{
        "customer_id": deterministic_id("CUS", index),
        "customer_segment": randomizer.choices(
            ["consumer", "small_business", "enterprise"], [0.72, 0.22, 0.06]
        )[0],
        "preferred_language_id": language_id[randomizer.choice(LANGUAGES)],
        "country": randomizer.choice(["IN", "US", "GB", "ES", "FR"]),
        "created_at": _iso(start - timedelta(days=randomizer.randint(30, 900))),
    } for index in range(1, customer_count + 1)]
    customers_by_language: dict[str, list[str]] = {language: [] for language in LANGUAGES}
    language_name_by_id = {identifier: name for name, identifier in language_id.items()}
    for customer in customer_rows:
        customers_by_language[language_name_by_id[customer["preferred_language_id"]]].append(
            customer["customer_id"]
        )

    agent_rows: list[dict[str, Any]] = []
    agent_skill_rows: list[dict[str, str]] = []
    agent_language_rows: list[dict[str, str]] = []
    for index in range(1, agents + 1):
        if index in {1, 2, 3}:
            chosen_skills, chosen_languages = SKILLS, LANGUAGES
            shift_start, shift_end = ("09:00", "18:00") if index == 1 else ("00:00", "23:59")
            capacity = 1 if index == 1 else 1000
            active = 1
        else:
            chosen_skills = randomizer.sample(SKILLS, k=randomizer.randint(2, len(SKILLS)))
            chosen_languages = randomizer.sample(LANGUAGES, k=randomizer.randint(1, 4))
            shift_start = randomizer.choice(["08:00", "09:00", "10:00", "12:00"])
            shift_end = randomizer.choice(["17:00", "18:00", "19:00", "21:00"])
            capacity = randomizer.randint(3, 8)
            active = int(randomizer.random() >= 0.05)
        agent_id = deterministic_id("AGT", index)
        agent_rows.append({
            "agent_id": agent_id,
            "agent_name": f"Agent {index:03d}",
            "timezone": timezone_name,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "active": active,
            "max_concurrency": capacity,
        })
        agent_skill_rows.extend({"agent_id": agent_id, "skill_id": skill_id[value]} for value in chosen_skills)
        agent_language_rows.extend({"agent_id": agent_id, "language_id": language_id[value]} for value in chosen_languages)
    agent_frame = pd.DataFrame([{
        **agent,
        "skills": "|".join(row["skill_id"] for row in agent_skill_rows
                           if row["agent_id"] == agent["agent_id"]),
        "languages": "|".join(row["language_id"] for row in agent_language_rows
                              if row["agent_id"] == agent["agent_id"]),
    } for agent in agent_rows])
    # Keep names in the routing view human-readable while SQL stores bridge-table IDs.
    for index, agent in enumerate(agent_frame.to_dict(orient="records")):
        agent["skills"] = "|".join(
            value for value in SKILLS
            if skill_id[value] in agent_frame.iloc[index]["skills"].split("|")
        )
        agent["languages"] = "|".join(
            value for value in LANGUAGES
            if language_id[value] in agent_frame.iloc[index]["languages"].split("|")
        )
        agent_frame.loc[index, "skills"] = agent["skills"]
        agent_frame.loc[index, "languages"] = agent["languages"]
    routing_agents = agent_frame.to_dict(orient="records")

    queue_rows = [{
        "queue_id": f"QUE-{index:02d}",
        "queue_name": f"{intent.replace('_', ' ').title()} Queue",
        "intent_id": intent_id[intent],
        "capacity": max(10, tickets // 5),
        "active": 1,
    } for index, intent in enumerate(INTENTS, 1)]
    queue_id = {intent: row["queue_id"] for intent, row in zip(INTENTS, queue_rows)}

    total_minutes = event_days * 24 * 60
    ticket_rows: list[dict[str, Any]] = []
    for index in range(1, tickets + 1):
        if index == 1:
            created = start + timedelta(hours=4)
            intent, channel, language, priority = "billing", "email", "English", "normal"
        elif index == 2:
            day_offset = min(1, event_days - 1)
            created = start + timedelta(days=day_offset, hours=17, minutes=30)
            intent, channel, language, priority = "delivery", "email", "Spanish", "normal"
        elif index == 3:
            created = start + timedelta(minutes=min(300, total_minutes - 1))
            intent, channel, language, priority = "technical_support", "chat", "Hindi", "high"
        elif index == 4:
            created = start + timedelta(minutes=min(120, total_minutes - 1))
            intent, channel, language, priority = "account_access", "phone", "English", "normal"
        elif index == 5:
            created = start + timedelta(minutes=min(180, total_minutes - 1))
            intent, channel, language, priority = "complaint", "email", "English", "high"
        else:
            created = start + timedelta(minutes=int(rng.integers(1, total_minutes)))
            intent = randomizer.choice(INTENTS)
            channel = randomizer.choices(CHANNELS, weights=[0.45, 0.40, 0.15])[0]
            language = randomizer.choice(LANGUAGES)
            priority = randomizer.choices(PRIORITIES, weights=[0.15, 0.55, 0.23, 0.07])[0]
            if intent == "complaint" and PRIORITY_RANK[priority] < PRIORITY_RANK["high"]:
                priority = "high"
        customer_id = randomizer.choice(customers_by_language[language])
        ticket_rows.append({
            "ticket_id": f"TKT-2026-{index:06d}",
            "customer_id": customer_id,
            "channel_id": channel_by_name[channel]["channel_id"],
            "intent_id": intent_id[intent],
            "language_id": language_id[language],
            "priority": priority,
            "queue_id": queue_id[intent],
            "status": "created",
            "subject": f"{intent.replace('_', ' ').title()} support request {index}",
            "created_at": _iso(created),
            "updated_at": _iso(created),
            "channel": channel,
            "intent": intent,
            "language": language,
        })
    ticket_rows.sort(key=lambda row: (row["created_at"], row["ticket_id"]))
    ticket_by_id = {row["ticket_id"]: row for row in ticket_rows}

    # Build result times first so routing capacity can account for when work is released.
    resolution_by_ticket: dict[str, dict[str, Any]] = {}
    release_at: dict[str, datetime] = {}
    for ticket in ticket_rows:
        _, target = _target_minutes(ticket["priority"], ticket["channel"])
        forced_resolved = ticket["ticket_id"] in {
            "TKT-2026-000001", "TKT-2026-000003", "TKT-2026-000004", "TKT-2026-000005"
        }
        resolved = forced_resolved or randomizer.random() < 0.90
        resolution_minutes = randomizer.randint(10, max(10, target * 2)) if resolved else None
        if ticket["ticket_id"] == "TKT-2026-000001" and resolved:
            resolution_minutes = 10
        if ticket["ticket_id"] == "TKT-2026-000003" and resolved:
            resolution_minutes = max(60, resolution_minutes)
        resolved_at = None
        if resolved:
            if resolution_minutes is None:
                raise RuntimeError("Resolved tickets must have a resolution duration.")
            resolved_at = add_business_minutes(
                _parse_datetime(ticket["created_at"]),
                resolution_minutes,
                **_calendar_args(calendars[ticket["channel"]]),
            )
            resolution_by_ticket[ticket["ticket_id"]] = {
                "resolution_id": f"RES-{ticket['ticket_id']}",
                "ticket_id": ticket["ticket_id"],
                "resolved_at": _iso(resolved_at),
                "resolution_code": randomizer.choice(
                    ["solved", "duplicate", "customer_guidance", "referred"]
                ),
                "resolved": 1,
                "resolution_minutes": resolution_minutes,
                "csat_score": randomizer.choices([1, 2, 3, 4, 5], [0.05, 0.08, 0.17, 0.35, 0.35])[0],
            }
            release_at[ticket["ticket_id"]] = resolved_at
        else:
            resolution_by_ticket[ticket["ticket_id"]] = {
                "resolution_id": f"RES-{ticket['ticket_id']}",
                "ticket_id": ticket["ticket_id"],
                "resolved_at": None,
                "resolution_code": "unresolved",
                "resolved": 0,
                "resolution_minutes": None,
                "csat_score": None,
            }
            release_at[ticket["ticket_id"]] = _parse_datetime(ticket["created_at"]) + timedelta(hours=4)

    assignment_rows: list[dict[str, Any]] = []
    assigned_agent: dict[str, str | None] = {}
    active_by_agent: dict[str, list[datetime]] = {agent["agent_id"]: [] for agent in agent_rows}
    active_by_queue: dict[str, list[datetime]] = {row["queue_id"]: [] for row in queue_rows}
    queue_capacity = {row["queue_id"]: row["capacity"] for row in queue_rows}
    for ticket in ticket_rows:
        created_at = _parse_datetime(ticket["created_at"])
        assign_at = created_at + timedelta(minutes=1)
        for agent_id_value, releases in active_by_agent.items():
            active_by_agent[agent_id_value] = [at for at in releases if at > assign_at]
        queue = ticket["queue_id"]
        active_by_queue[queue] = [at for at in active_by_queue[queue] if at > assign_at]
        workload = {key: len(value) for key, value in active_by_agent.items()}
        decision = route_ticket(
            ticket,
            routing_agents,
            now=assign_at,
            current_workloads=workload,
            queue_backlog=len(active_by_queue[queue]),
            queue_capacity=queue_capacity[queue],
        )
        agent_id_value = decision["agent_id"]
        assigned_agent[ticket["ticket_id"]] = agent_id_value
        released = release_at[ticket["ticket_id"]]
        if agent_id_value is None:
            assignment_status = "overflow"
            ticket["status"] = "pending"
            ticket["updated_at"] = _iso(assign_at)
            resolution_by_ticket[ticket["ticket_id"]].update({
                "resolved_at": None,
                "resolution_code": "unresolved",
                "resolved": 0,
                "resolution_minutes": None,
                "csat_score": None,
            })
            release_at[ticket["ticket_id"]] = created_at + timedelta(hours=4)
            active_by_queue[queue].append(release_at[ticket["ticket_id"]])
        else:
            assignment_status = "assigned"
            active_by_agent[agent_id_value].append(released)
            active_by_queue[queue].append(released)
            ticket["status"] = "resolved" if resolution_by_ticket[ticket["ticket_id"]]["resolved"] else "pending"
            ticket["updated_at"] = _iso(release_at[ticket["ticket_id"]])
        assignment_rows.append({
            "assignment_id": f"ASN-{ticket['ticket_id']}-001",
            "ticket_id": ticket["ticket_id"],
            "agent_id": agent_id_value,
            "queue_id": queue,
            "assigned_at": _iso(assign_at),
            "unassigned_at": _iso(released) if agent_id_value is not None else None,
            "assignment_status": assignment_status,
            "decision_reason": decision["decision_reason"],
            "assignment_number": 1,
        })
        ticket["routing_outcome"] = "assigned" if agent_id_value is not None else "queue_overflow"
        ticket["assigned_at"] = _iso(assign_at)

    # Messages are grouped by ticket so their required per-ticket counters remain stable.
    message_counts = np.bincount(rng.integers(0, tickets, size=messages), minlength=tickets)
    message_rows: list[dict[str, Any]] = []
    ticket_order = sorted(ticket_by_id)
    for ticket_index, count in enumerate(message_counts):
        ticket = ticket_by_id[ticket_order[ticket_index]]
        created_at = _parse_datetime(ticket["created_at"])
        for sequence in range(1, int(count) + 1):
            message_at = created_at + timedelta(minutes=int(rng.integers(0, 7 * 24 * 60 + 1)))
            message_rows.append({
                "message_id": f"MSG-{ticket['ticket_id']}-{sequence:03d}",
                "ticket_id": ticket["ticket_id"],
                "channel_id": ticket["channel_id"],
                "sender_type": randomizer.choices(["customer", "agent", "system"], [0.58, 0.37, 0.05])[0],
                "body": f"Synthetic support message {sequence} for {ticket['ticket_id']}",
                "created_at": _iso(message_at),
            })

    sla_rows: list[dict[str, Any]] = []
    ground_truth_rows: list[dict[str, Any]] = []
    initial_assignment_by_ticket = {row["ticket_id"]: row for row in assignment_rows}
    for index, ticket in enumerate(ticket_rows):
        calendar = calendars[ticket["channel"]]
        calendar_options = _calendar_args(calendar)
        created_at = _parse_datetime(ticket["created_at"])
        response_target, resolution_target = _target_minutes(ticket["priority"], ticket["channel"])
        response_work = randomizer.randint(1, max(1, response_target * 2))
        response_at = add_business_minutes(created_at, response_work, **calendar_options)
        response_sla = calculate_sla(created_at, response_target, **calendar_options)
        response_breach = response_at > response_sla.due_at
        sla_rows.append({
            "sla_id": f"SLA-{ticket['ticket_id']}-FR",
            "ticket_id": ticket["ticket_id"],
            "sla_type": "first_response",
            "target_minutes": response_target,
            "started_at": _iso(created_at),
            "due_at": _iso(response_sla.due_at),
            "actual_at": _iso(response_at),
            "breached": int(response_breach),
            "breach_minutes": breach_minutes(response_sla.due_at, response_at, **calendar_options),
            "escalation_at": _iso(max(
                created_at,
                response_sla.due_at - timedelta(minutes=5),
            )) if ticket["priority"] == "critical" else None,
            "calendar_id": calendar["calendar_id"],
        })
        resolution = resolution_by_ticket[ticket["ticket_id"]]
        resolution_sla = calculate_sla(created_at, resolution_target, **calendar_options)
        actual_resolution = _parse_datetime(resolution["resolved_at"]) if resolution["resolved_at"] else None
        snapshot_at = start + timedelta(days=event_days)
        is_breached = (
            actual_resolution > resolution_sla.due_at
            if actual_resolution is not None
            else resolution_sla.due_at <= snapshot_at
        )
        resolution_breach_minutes = (
            breach_minutes(resolution_sla.due_at, actual_resolution, **calendar_options)
            if actual_resolution is not None
            else business_minutes_between(resolution_sla.due_at, snapshot_at, **calendar_options)
            if is_breached
            else 0
        )
        sla_rows.append({
            "sla_id": f"SLA-{ticket['ticket_id']}",
            "ticket_id": ticket["ticket_id"],
            "sla_type": "resolution",
            "target_minutes": resolution_target,
            "started_at": _iso(created_at),
            "due_at": _iso(resolution_sla.due_at),
            "actual_at": _iso(actual_resolution) if actual_resolution else None,
            "breached": int(is_breached),
            "breach_minutes": resolution_breach_minutes,
            "escalation_at": _iso(resolution_sla.due_at - timedelta(minutes=15)) if ticket["priority"] == "critical" else None,
            "calendar_id": calendar["calendar_id"],
        })
        ground_truth_rows.append({
            "ticket_id": ticket["ticket_id"],
            "expected_intent": ticket["intent"],
            "expected_priority": "high" if ticket["intent"] == "complaint" and ticket["priority"] == "low" else ticket["priority"],
            "expected_queue_id": ticket["queue_id"],
            "expected_routing_outcome": ticket["routing_outcome"],
            "expected_agent_id": assigned_agent[ticket["ticket_id"]],
            "expected_decision_reason": initial_assignment_by_ticket[ticket["ticket_id"]]["decision_reason"],
            "expected_sla_outcome": "breached" if is_breached else "met",
            "anomaly_type": None,
            "expected_validation": "VALID",
        })

    event_rows: list[dict[str, Any]] = []
    for ticket in ticket_rows:
        ticket_id = ticket["ticket_id"]
        created_at = _parse_datetime(ticket["created_at"])
        assign_at = _parse_datetime(ticket["assigned_at"])
        _event(event_rows, ticket_id, "created", created_at, None, "created", "Ticket created")
        _event(event_rows, ticket_id, "opened", created_at + timedelta(seconds=30),
               "created", "open", "Ticket opened")
        agent_id_value = assigned_agent[ticket_id]
        if agent_id_value is not None:
            _event(event_rows, ticket_id, "assigned", assign_at, "open", "assigned",
                   "Initial routing decision", agent_id_value)
            resolution = resolution_by_ticket[ticket_id]
            if resolution["resolved"]:
                _event(event_rows, ticket_id, "work_started", assign_at + timedelta(seconds=30),
                       "assigned", "in_progress", "Agent began handling the ticket", agent_id_value)
                resolved_at = _parse_datetime(resolution["resolved_at"])
                _event(event_rows, ticket_id, "resolved", resolved_at, "in_progress", "resolved",
                       resolution["resolution_code"], agent_id_value)
                if ticket_id == "TKT-2026-000004":
                    pass
                elif randomizer.random() < 0.25:
                    closed_at = resolved_at + timedelta(minutes=1)
                    _event(event_rows, ticket_id, "closed", closed_at, "resolved", "closed",
                           "Closed after resolution", agent_id_value)
                    ticket["status"] = "closed"
                    ticket["updated_at"] = _iso(closed_at)
            else:
                _event(event_rows, ticket_id, "pending", assign_at + timedelta(seconds=30),
                       "assigned", "pending", "Awaiting follow-up")
        else:
            _event(event_rows, ticket_id, "pending", assign_at, "open", "pending",
                   "No eligible agent; kept in queue")
    for sla_event in sla_rows:
        if int(sla_event["breached"]) and sla_event["escalation_at"]:
            ticket_id = sla_event["ticket_id"]
            _event(
                event_rows,
                ticket_id,
                "escalated",
                _parse_datetime(sla_event["escalation_at"]),
                None,
                None,
                f"{sla_event['sla_type']} SLA escalation deadline reached",
                assigned_agent[ticket_id],
            )

    anomaly_rows = (
        _with_anomalies(
            ticket_rows,
            assignment_rows,
            sla_rows,
            event_rows,
            list(resolution_by_ticket.values()),
            agent_rows,
            ground_truth_rows,
            seed,
            channel_rows,
        )
        if inject_anomalies
        else []
    )
    for event_index, event in enumerate(event_rows, 1):
        event["event_id"] = f"EVT-{event_index:08d}"
    events_by_ticket_type: dict[tuple[str, str], list[str]] = {}
    for event in event_rows:
        events_by_ticket_type.setdefault(
            (str(event["ticket_id"]), str(event["event_type"])), []
        ).append(str(event["event_id"]))
    assignments_by_id = {str(row["assignment_id"]): row for row in assignment_rows}
    slas_by_id = {str(row["sla_id"]): row for row in sla_rows}
    for anomaly in anomaly_rows:
        ticket_id = str(anomaly["ticket_id"])
        references = {
            "ticket_event_ids": [
                event_id
                for event_type in anomaly.pop("_ticket_event_types")
                for event_id in events_by_ticket_type.get((ticket_id, event_type), [])
            ],
            "assignment_ids": [
                assignment_id
                for assignment_id in anomaly.pop("_assignment_ids")
                if assignment_id in assignments_by_id
            ],
            "sla_ids": [
                sla_id for sla_id in anomaly.pop("_sla_ids") if sla_id in slas_by_id
            ],
        }
        anomaly["event_references"] = json.dumps(references, sort_keys=True)
    workload_snapshot = max(_parse_datetime(ticket["created_at"]) for ticket in ticket_rows) + timedelta(minutes=1)
    current_workloads = {agent["agent_id"]: 0 for agent in agent_rows}
    for assignment in assignment_rows:
        if assignment["agent_id"] is None:
            continue
        assigned_at = _parse_datetime(assignment["assigned_at"])
        unassigned_at = _parse_datetime(assignment["unassigned_at"]) if assignment["unassigned_at"] else None
        if assigned_at <= workload_snapshot and (unassigned_at is None or unassigned_at > workload_snapshot):
            current_workloads[assignment["agent_id"]] += 1
    for agent in agent_rows:
        agent["current_workload"] = current_workloads[agent["agent_id"]]
    # The SQL schema stores canonical normalized tables only; helper columns are export-only.
    feature_frames = {
        "business_calendars": pd.DataFrame(calendar_rows),
        "calendar_business_days": pd.DataFrame(calendar_day_rows),
        "calendar_holidays": pd.DataFrame(holiday_rows),
        "customers": pd.DataFrame(customer_rows),
        "channels": pd.DataFrame(channel_rows),
        "languages": pd.DataFrame(language_rows),
        "intents": pd.DataFrame(intent_rows),
        "priority_rules": pd.DataFrame(priority_rows),
        "agents": pd.DataFrame(agent_rows),
        "skills": pd.DataFrame(skill_rows),
        "agent_skills": pd.DataFrame(agent_skill_rows),
        "agent_languages": pd.DataFrame(agent_language_rows),
        "queues": pd.DataFrame(queue_rows),
        "tickets": pd.DataFrame([{key: value for key, value in row.items()
                                  if key not in {"channel", "intent", "language", "routing_outcome", "assigned_at"}}
                                 for row in ticket_rows]),
        "messages": pd.DataFrame(message_rows, columns=[
            "message_id", "ticket_id", "channel_id", "sender_type", "body", "created_at",
        ]),
        "assignment_events": pd.DataFrame(assignment_rows),
        "sla_events": pd.DataFrame(sla_rows),
        "ticket_events": pd.DataFrame(event_rows),
        "resolutions": pd.DataFrame(resolution_by_ticket.values()),
        "anomalies": pd.DataFrame(anomaly_rows, columns=ANOMALY_GROUND_TRUTH_COLUMNS),
        "ground_truth_labels": pd.DataFrame(ground_truth_rows),
    }

    # Export feature/source tables as CSV and labels to a separate directory.
    for table_name, frame in feature_frames.items():
        if table_name in {"ground_truth_labels", "anomalies"}:
            continue
        frame.to_csv(out / f"{table_name}.csv", index=False, lineterminator="\n")
        frame.to_json(json_dir / f"{table_name}.json", orient="records", indent=2, force_ascii=False)
    pd.DataFrame(ground_truth_rows).to_csv(truth_dir / "ground_truth.csv", index=False, lineterminator="\n")
    pd.DataFrame(anomaly_rows, columns=ANOMALY_GROUND_TRUTH_COLUMNS).to_csv(
        truth_dir / "anomalies.csv", index=False, lineterminator="\n",
    )
    pd.DataFrame(ground_truth_rows).to_json(
        truth_dir / "ground_truth.json", orient="records", indent=2, force_ascii=False,
    )
    pd.DataFrame(anomaly_rows, columns=ANOMALY_GROUND_TRUTH_COLUMNS).to_json(
        truth_dir / "anomalies.json", orient="records", indent=2, force_ascii=False,
    )
    database_path = out / "tickets.sqlite"
    if database_path.exists():
        database_path.unlink()
    _write_database(database_path, feature_frames)
    sql_dir = out / "sql"
    sql_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        seed_sql = "\n".join(connection.iterdump()) + "\n"
    (sql_dir / "seed_data.sql").write_text(seed_sql, encoding="utf-8")

    output_files = [
        *(out / f"{name}.csv" for name in feature_frames if name not in {"ground_truth_labels", "anomalies"}),
        *(json_dir / f"{name}.json" for name in feature_frames if name not in {"ground_truth_labels", "anomalies"}),
        truth_dir / "ground_truth.csv",
        truth_dir / "ground_truth.json",
        truth_dir / "anomalies.csv",
        truth_dir / "anomalies.json",
        database_path,
        sql_dir / "seed_data.sql",
    ]
    checksums = {}
    for path in sorted(output_files, key=lambda item: str(item.relative_to(out))):
        checksums[str(path.relative_to(out)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "seed": seed,
        "configuration": {
            "tickets": tickets,
            "agents": agents,
            "messages": messages,
            "event_days": event_days,
            "start_date": start_date,
            "timezone_name": timezone_name,
            "inject_anomalies": inject_anomalies,
        },
        "counts": {
            name: int(len(frame)) for name, frame in feature_frames.items()
        },
        "checksums_sha256": checksums,
        "versions": {
            "python": sys.version.split()[0],
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "schema_version": SCHEMA_VERSION,
        },
        "notes": [
            "Ground truth and anomaly labels are stored separately from source feature CSVs.",
            "Reproducibility is guaranteed for the same configuration, seed, Python, pandas, and NumPy versions.",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Generated {tickets:,} tickets, {agents:,} agents, and {messages:,} messages.")
    print(f"Output: {out.resolve()}")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    config = load_config()
    parser.add_argument("--seed", type=int, default=config.seed)
    parser.add_argument("--tickets", type=int, default=config.tickets)
    parser.add_argument("--agents", type=int, default=config.agents)
    parser.add_argument("--messages", type=int, default=config.messages)
    parser.add_argument("--event-days", type=int, default=config.event_days)
    parser.add_argument("--start-date", default=config.start_date)
    parser.add_argument("--output-dir", default=config.output_dir)
    parser.add_argument("--timezone", default=config.timezone_name)
    parser.add_argument(
        "--no-anomalies",
        action="store_false",
        dest="inject_anomalies",
        help="Generate only valid baseline data without injecting the five challenge anomalies.",
    )
    parser.set_defaults(inject_anomalies=True)
    arguments = parser.parse_args()
    generate(
        seed=arguments.seed,
        tickets=arguments.tickets,
        agents=arguments.agents,
        messages=arguments.messages,
        event_days=arguments.event_days,
        start_date=arguments.start_date,
        output_dir=arguments.output_dir,
        timezone_name=arguments.timezone,
        inject_anomalies=arguments.inject_anomalies,
    )
