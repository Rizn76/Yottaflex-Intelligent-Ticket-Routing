"""Validate generated data while distinguishing intentional anomalies from defects."""
from __future__ import annotations

import json
import argparse
import hashlib
from contextlib import closing
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from config import load_config
from src.lifecycle import ALLOWED_TRANSITIONS, detect_transfer_loop
from src.routing import agent_is_on_shift
from src.sla_engine import breach_minutes, calculate_sla


TABLE_FILES = [
    "business_calendars", "calendar_business_days", "calendar_holidays", "customers",
    "channels", "languages", "intents", "priority_rules", "agents", "skills",
    "agent_skills", "agent_languages", "queues", "tickets", "messages",
    "assignment_events", "sla_events", "ticket_events", "resolutions",
]
PRIMARY_KEYS = {
    "business_calendars": ["calendar_id"],
    "customers": ["customer_id"],
    "channels": ["channel_id"],
    "languages": ["language_id"],
    "intents": ["intent_id"],
    "priority_rules": ["priority_rule_id"],
    "agents": ["agent_id"],
    "skills": ["skill_id"],
    "queues": ["queue_id"],
    "tickets": ["ticket_id"],
    "messages": ["message_id"],
    "assignment_events": ["assignment_id"],
    "sla_events": ["sla_id"],
    "ticket_events": ["event_id"],
    "resolutions": ["resolution_id"],
    "anomalies": ["anomaly_id"],
    "ground_truth": ["ticket_id"],
}
REQUIRED_COLUMNS = {
    "tickets": ["ticket_id", "customer_id", "channel_id", "intent_id", "language_id",
                "priority", "queue_id", "status", "created_at", "updated_at"],
    "messages": ["message_id", "ticket_id", "channel_id", "sender_type", "created_at"],
    "assignment_events": ["assignment_id", "ticket_id", "queue_id", "assigned_at",
                          "assignment_status", "decision_reason", "assignment_number"],
    "sla_events": ["sla_id", "ticket_id", "sla_type", "target_minutes", "started_at",
                   "due_at", "breached", "breach_minutes", "calendar_id"],
    "ticket_events": ["event_id", "ticket_id", "event_type", "event_at", "details"],
}
VALID_PRIORITIES = {"low", "normal", "high", "critical"}
VALID_STATUSES = {"created", "open", "assigned", "in_progress", "pending", "resolved", "closed"}
ANOMALY_EXPECTATIONS = {
    "agent_over_capacity": "CAPACITY_VIOLATION",
    "sla_ignores_channel_calendar": "SLA_DEADLINE_MISMATCH",
    "reopen_loses_history": "MISSING_LIFECYCLE_HISTORY",
    "transfer_loop": "TRANSFER_LOOP",
    "priority_safety_contradiction": "PRIORITY_CONTRADICTION",
}


def _read_csv(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Required generated table is missing: {path}")
    return pd.read_csv(path, keep_default_na=True) if columns is None else pd.read_csv(path, keep_default_na=True, usecols=columns)


def _is_missing(value: Any) -> bool:
    return value is None or pd.isna(value)


def _to_datetime(value: Any) -> datetime | None:
    if _is_missing(value):
        return None
    return datetime.fromisoformat(str(value))


def _validate_capacity(
    assignments: pd.DataFrame,
    agents: pd.DataFrame,
    expected_types: dict[str, str],
    snapshot_at: datetime,
    errors: list[str],
    detected: set[str],
) -> None:
    capacity_by_agent = agents.set_index("agent_id")["max_concurrency"].astype(int).to_dict()
    agent_groups = assignments[assignments["agent_id"].notna()].groupby("agent_id")
    for agent_id, group in agent_groups:
        changes = []
        for row in group.to_dict(orient="records"):
            assigned_at = _to_datetime(row["assigned_at"])
            unassigned_at = _to_datetime(row["unassigned_at"]) or snapshot_at
            if assigned_at is None or unassigned_at <= assigned_at:
                continue
            changes.append((assigned_at, 1, row))
            changes.append((unassigned_at, -1, row))
        current = 0
        for _, change, row in sorted(changes, key=lambda item: (item[0], item[1], item[2]["assignment_id"])):
            current += change
            if current > capacity_by_agent[str(agent_id)]:
                anomaly_type = expected_types.get(str(row["ticket_id"]))
                if anomaly_type == "agent_over_capacity":
                    detected.add("agent_over_capacity")
                else:
                    errors.append(
                        f"Agent {agent_id} has {current} concurrent assignments "
                        f"above capacity {capacity_by_agent[str(agent_id)]}."
                    )


def _validate_slas(
    sla: pd.DataFrame,
    calendars: pd.DataFrame,
    business_days: pd.DataFrame,
    holidays: pd.DataFrame,
    expected_types: dict[str, str],
    snapshot_at: datetime,
    errors: list[str],
    detected: set[str],
) -> None:
    calendar_rows = calendars.set_index("calendar_id").to_dict(orient="index")
    days_by_calendar: dict[str, set[int]] = defaultdict(set)
    holidays_by_calendar: dict[str, set[date]] = defaultdict(set)
    for row in business_days.to_dict(orient="records"):
        days_by_calendar[str(row["calendar_id"])].add(int(row["weekday"]))
    for row in holidays.to_dict(orient="records"):
        holidays_by_calendar[str(row["calendar_id"])].add(date.fromisoformat(str(row["holiday_date"])))

    for row in sla.to_dict(orient="records"):
        calendar = calendar_rows[str(row["calendar_id"])]
        options = {
            "timezone_name": str(calendar["timezone"]),
            "business_start": time.fromisoformat(str(calendar["business_hours_start"])),
            "business_end": time.fromisoformat(str(calendar["business_hours_end"])),
            "weekdays": frozenset(days_by_calendar[str(row["calendar_id"])]),
            "holidays": frozenset(holidays_by_calendar[str(row["calendar_id"])]),
        }
        started = _to_datetime(row["started_at"])
        stored_due = _to_datetime(row["due_at"])
        if started is None or stored_due is None:
            errors.append(f"SLA {row['sla_id']} has a missing start/deadline timestamp.")
            continue
        expected_due = calculate_sla(started, int(row["target_minutes"]), **options).due_at
        if expected_due != (stored_due.replace(tzinfo=expected_due.tzinfo) if stored_due.tzinfo is None else stored_due):
            if expected_types.get(str(row["ticket_id"])) == "sla_ignores_channel_calendar":
                detected.add("sla_ignores_channel_calendar")
            else:
                errors.append(f"SLA {row['sla_id']} deadline does not match its saved calendar and target.")
        actual = _to_datetime(row["actual_at"])
        observed_at = actual or snapshot_at
        expected_breach = (
            actual > expected_due
            if actual is not None
            else snapshot_at >= expected_due
        )
        is_calendar_anomaly = expected_types.get(str(row["ticket_id"])) == "sla_ignores_channel_calendar"
        if bool(int(row["breached"])) != expected_breach and not is_calendar_anomaly:
            errors.append(f"SLA {row['sla_id']} breach flag is inconsistent with its actual timestamp.")
        breach_value = int(row["breach_minutes"])
        expected_breach_minutes = breach_minutes(expected_due, observed_at, **options) if expected_breach else 0
        if breach_value != expected_breach_minutes and not is_calendar_anomaly:
            errors.append(f"SLA {row['sla_id']} breach duration does not match its business calendar.")


def validate(data_dir: str | Path | None = None) -> bool:
    path = Path(load_config().output_dir if data_dir is None else data_dir)
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Dataset manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    configuration = manifest["configuration"]
    inject_anomalies = bool(configuration.get("inject_anomalies", True))
    snapshot_at = datetime.fromisoformat(configuration["start_date"]).replace(
        tzinfo=timezone.utc,
    ) + timedelta(days=int(configuration["event_days"]))
    tables = {name: _read_csv(path / f"{name}.csv") for name in TABLE_FILES}
    anomalies = _read_csv(path / "ground_truth" / "anomalies.csv")
    ground_truth = _read_csv(path / "ground_truth" / "ground_truth.csv")

    errors: list[str] = []
    detected: set[str] = set()
    expected_types = {
        str(row["ticket_id"]): str(row["anomaly_type"])
        for row in anomalies.to_dict(orient="records")
    }

    for name, columns in REQUIRED_COLUMNS.items():
        frame = tables[name]
        for column in columns:
            if column not in frame.columns:
                errors.append(f"{name} is missing required column {column}.")
            elif frame[column].isna().any():
                errors.append(f"{name}.{column} contains missing required values.")
    required_anomaly_columns = {
        "anomaly_id",
        "ticket_id",
        "anomaly_type",
        "description",
        "expected_validation",
        "ground_truth_label",
        "affected_entity_ids",
        "event_references",
        "reference_timestamp",
        "expected_behavior",
        "observed_behavior",
        "severity",
        "seed",
    }
    if not required_anomaly_columns <= set(anomalies.columns):
        errors.append(
            "Anomaly ground truth is missing columns: "
            f"{sorted(required_anomaly_columns - set(anomalies.columns))}."
        )

    for name, key_columns in PRIMARY_KEYS.items():
        frame = anomalies if name == "anomalies" else ground_truth if name == "ground_truth" else tables.get(name)
        if frame is None:
            continue
        if frame.duplicated(key_columns).any():
            errors.append(f"{name} has duplicate primary-key values in {key_columns}.")

    tickets = tables["tickets"]
    agents = tables["agents"]
    assignments = tables["assignment_events"]
    events = tables["ticket_events"]
    sla = tables["sla_events"]
    customers = set(tables["customers"]["customer_id"].astype(str))
    customer_languages = tables["customers"].set_index("customer_id")["preferred_language_id"].to_dict()
    tickets_by_id = tickets.set_index("ticket_id").to_dict(orient="index")
    ticket_ids = set(tickets["ticket_id"].astype(str))
    agent_ids = set(agents["agent_id"].astype(str))
    channel_ids = set(tables["channels"]["channel_id"].astype(str))
    intent_ids = set(tables["intents"]["intent_id"].astype(str))
    language_ids = set(tables["languages"]["language_id"].astype(str))
    queue_ids = set(tables["queues"]["queue_id"].astype(str))

    foreign_keys = [
        ("tickets.customer_id", tickets, "customer_id", customers),
        ("tickets.channel_id", tickets, "channel_id", channel_ids),
        ("tickets.intent_id", tickets, "intent_id", intent_ids),
        ("tickets.language_id", tickets, "language_id", language_ids),
        ("tickets.queue_id", tickets, "queue_id", queue_ids),
    ]
    for table_name, frame, column, valid_ids in foreign_keys:
        invalid = set(frame[column].dropna().astype(str)) - valid_ids
        if invalid:
            errors.append(f"{table_name} has {len(invalid)} orphan references.")
    for name in ["messages", "assignment_events", "sla_events", "ticket_events", "resolutions"]:
        invalid = set(tables[name]["ticket_id"].dropna().astype(str)) - ticket_ids
        if invalid:
            errors.append(f"{name}.ticket_id has {len(invalid)} orphan references.")
    invalid_agents = set(assignments["agent_id"].dropna().astype(str)) - agent_ids
    if invalid_agents:
        errors.append(f"assignment_events.agent_id has {len(invalid_agents)} orphan references.")
    if tables["calendar_business_days"].duplicated(["calendar_id", "weekday"]).any():
        errors.append("calendar_business_days contains duplicate bridge records.")
    if tables["calendar_holidays"].duplicated(["calendar_id", "holiday_date"]).any():
        errors.append("calendar_holidays contains duplicate holiday records.")
    if tables["agent_skills"].duplicated(["agent_id", "skill_id"]).any():
        errors.append("agent_skills contains duplicate bridge records.")
    if tables["agent_languages"].duplicated(["agent_id", "language_id"]).any():
        errors.append("agent_languages contains duplicate bridge records.")
    queue_intents = tables["queues"].set_index("queue_id")["intent_id"].astype(str).to_dict()
    for row in tickets.to_dict(orient="records"):
        if queue_intents.get(str(row["queue_id"])) != str(row["intent_id"]):
            errors.append(f"Ticket {row['ticket_id']} is in a queue for a different intent.")
        preferred_language = customer_languages.get(str(row["customer_id"]))
        if not _is_missing(preferred_language) and str(row["language_id"]) != str(preferred_language):
            errors.append(f"Ticket {row['ticket_id']} language differs from its customer's preference.")

    invalid_priorities = set(tickets["priority"].dropna().astype(str)) - VALID_PRIORITIES
    invalid_statuses = set(tickets["status"].dropna().astype(str)) - VALID_STATUSES
    if invalid_priorities:
        errors.append(f"tickets has invalid priorities: {sorted(invalid_priorities)}.")
    if invalid_statuses:
        errors.append(f"tickets has invalid statuses: {sorted(invalid_statuses)}.")
    if not tickets.empty:
        for row in tickets.to_dict(orient="records"):
            created, updated = _to_datetime(row["created_at"]), _to_datetime(row["updated_at"])
            if created is not None and updated is not None and updated < created:
                errors.append(f"Ticket {row['ticket_id']} is updated before creation.")
    for row in tables["messages"].to_dict(orient="records"):
        ticket = tickets_by_id.get(str(row["ticket_id"]))
        if ticket and str(row["channel_id"]) != str(ticket["channel_id"]):
            errors.append(f"Message {row['message_id']} channel differs from its ticket channel.")
        if ticket and _to_datetime(row["created_at"]) < _to_datetime(ticket["created_at"]):
            errors.append(f"Message {row['message_id']} predates its ticket.")

    workload_snapshot = max(_to_datetime(value) for value in tickets["created_at"]) + timedelta(minutes=1)
    _validate_capacity(assignments, agents, expected_types, workload_snapshot, errors, detected)
    queue_capacities = tables["queues"].set_index("queue_id")["capacity"].astype(int).to_dict()
    for queue_id, group in assignments.groupby("queue_id"):
        changes = []
        for row in group.to_dict(orient="records"):
            started = _to_datetime(row["assigned_at"])
            ended = _to_datetime(row["unassigned_at"]) or workload_snapshot
            if started is not None and ended > started:
                changes.extend([(started, 1), (ended, -1)])
        current = 0
        for _, change in sorted(changes, key=lambda item: (item[0], item[1])):
            current += change
            if current > queue_capacities[str(queue_id)]:
                errors.append(f"Queue {queue_id} has {current} active assignments above capacity.")
                break
    expected_workload = {str(agent_id): 0 for agent_id in agents["agent_id"]}
    for row in assignments.to_dict(orient="records"):
        if _is_missing(row["agent_id"]):
            continue
        assigned_at = _to_datetime(row["assigned_at"])
        unassigned_at = _to_datetime(row["unassigned_at"])
        if assigned_at <= workload_snapshot and (unassigned_at is None or unassigned_at > workload_snapshot):
            expected_workload[str(row["agent_id"])] += 1
    observed_workload = agents.set_index("agent_id")["current_workload"].astype(int).to_dict()
    for agent_id, expected in expected_workload.items():
        if observed_workload[agent_id] != expected:
            errors.append(f"Agent {agent_id} current workload does not match the assignment history.")

    # Validate routing against normalized skill/language bridge tables.
    skill_names = tables["skills"].set_index("skill_id")["skill_name"].to_dict()
    intent_names = tables["intents"].set_index("intent_id")["intent_name"].to_dict()
    language_names = tables["languages"].set_index("language_id")["language_name"].to_dict()
    skills_by_agent: dict[str, set[str]] = defaultdict(set)
    languages_by_agent: dict[str, set[str]] = defaultdict(set)
    for row in tables["agent_skills"].to_dict(orient="records"):
        skills_by_agent[str(row["agent_id"])].add(skill_names[str(row["skill_id"])])
    for row in tables["agent_languages"].to_dict(orient="records"):
        languages_by_agent[str(row["agent_id"])].add(language_names[str(row["language_id"])])
    agent_active = agents.set_index("agent_id")["active"].astype(int).to_dict()
    agent_schedule = agents.set_index("agent_id").to_dict(orient="index")
    for row in assignments.to_dict(orient="records"):
        agent_id = row["agent_id"]
        if _is_missing(agent_id):
            if row["assignment_status"] != "overflow":
                errors.append(f"Unassigned record {row['assignment_id']} is not marked as queue overflow.")
            continue
        ticket = tickets_by_id[str(row["ticket_id"])]
        agent_id = str(agent_id)
        if not agent_active.get(agent_id, 0):
            errors.append(f"Inactive agent {agent_id} received assignment {row['assignment_id']}.")
        assigned_at = _to_datetime(row["assigned_at"])
        if assigned_at is not None and not agent_is_on_shift(agent_schedule[agent_id], assigned_at):
            errors.append(f"Agent {agent_id} was off shift for assignment {row['assignment_id']}.")
        if intent_names[str(ticket["intent_id"])] not in skills_by_agent[agent_id]:
            errors.append(f"Agent {agent_id} lacks the ticket intent skill for {row['ticket_id']}.")
        if language_names[str(ticket["language_id"])] not in languages_by_agent[agent_id]:
            if str(ticket["priority"]) == "critical":
                errors.append(f"Critical ticket {row['ticket_id']} was routed across a language mismatch.")
            elif "language-mismatch fallback" not in str(row["decision_reason"]) and row["decision_reason"] != "injected anomaly: transfer cycle":
                errors.append(f"Assignment {row['assignment_id']} has an undocumented language mismatch.")

    # Event timelines must be chronological and state-contiguous; the known missing-history
    # scenario is reported as an expected anomaly rather than an accidental failure.
    for ticket_id, group in events.groupby("ticket_id"):
        records = sorted(group.to_dict(orient="records"), key=lambda row: _to_datetime(row["event_at"]))
        ticket = tickets_by_id[str(ticket_id)]
        if any(_to_datetime(right["event_at"]) < _to_datetime(left["event_at"])
               for left, right in zip(records, records[1:])):
            errors.append(f"Ticket {ticket_id} lifecycle events are out of timestamp order.")
        created_events = [row for row in records if row["event_type"] == "created"]
        if len(created_events) > 1:
            errors.append(f"Ticket {ticket_id} has duplicate creation lifecycle events.")
        if records and not any(row["event_type"] == "created" for row in records):
            if expected_types.get(str(ticket_id)) == "reopen_loses_history":
                detected.add("reopen_loses_history")
            else:
                errors.append(f"Ticket {ticket_id} has no creation event in its lifecycle history.")
        for previous, current in zip(records, records[1:]):
            previous_state, from_state = previous["to_state"], current["from_state"]
            if not _is_missing(from_state) and not _is_missing(previous_state) and from_state != previous_state:
                if expected_types.get(str(ticket_id)) == "reopen_loses_history":
                    detected.add("reopen_loses_history")
                else:
                    errors.append(f"Ticket {ticket_id} lifecycle state history is discontinuous.")
        for event in records:
            from_state, to_state = event["from_state"], event["to_state"]
            if not _is_missing(to_state):
                expected_event_type = {
                    "assigned": "assigned",
                    "in_progress": "work_started",
                    "pending": "pending",
                    "resolved": "resolved",
                    "closed": "closed",
                }.get(str(to_state))
                if str(to_state) == "open" and not _is_missing(from_state):
                    expected_event_type = (
                        "reopened"
                        if str(from_state) == "resolved"
                        else "opened"
                        if str(from_state) == "created"
                        else "state_changed"
                    )
                if (
                    expected_event_type is not None
                    and event["event_type"] != expected_event_type
                    and event["event_type"] not in {"transferred", "capacity_violation"}
                ):
                    errors.append(
                        f"Ticket {ticket_id} event {event['event_id']} does not match "
                        f"transition {from_state} -> {to_state}."
                    )
            if _is_missing(from_state) or _is_missing(to_state) or from_state == to_state:
                continue
            if str(to_state) not in ALLOWED_TRANSITIONS.get(str(from_state), set()):
                if expected_types.get(str(ticket_id)) == "reopen_loses_history":
                    detected.add("reopen_loses_history")
                else:
                    errors.append(
                        f"Ticket {ticket_id} has invalid lifecycle transition "
                        f"{from_state} -> {to_state}."
                    )
        stateful_events = [
            row for row in records
            if not _is_missing(row["to_state"])
        ]
        if stateful_events and str(stateful_events[-1]["to_state"]) != str(ticket["status"]):
            errors.append(
                f"Ticket {ticket_id} current status {ticket['status']} does not match "
                f"its last lifecycle state {stateful_events[-1]['to_state']}."
            )

    for ticket_id, group in assignments.groupby("ticket_id"):
        rows = sorted(group.to_dict(orient="records"), key=lambda row: int(row["assignment_number"]))
        agent_history = [str(row["agent_id"]) for row in rows if not _is_missing(row["agent_id"])]
        if detect_transfer_loop(agent_history):
            if expected_types.get(str(ticket_id)) == "transfer_loop":
                detected.add("transfer_loop")
            else:
                errors.append(f"Ticket {ticket_id} contains an undocumented transfer loop.")

    calendars, business_days, holidays = (
        tables["business_calendars"],
        tables["calendar_business_days"],
        tables["calendar_holidays"],
    )
    _validate_slas(
        sla, calendars, business_days, holidays, expected_types, snapshot_at,
        errors, detected,
    )

    priority_rules = tables["priority_rules"].set_index(["priority", "channel_id"])
    truth_by_ticket = ground_truth.set_index("ticket_id").to_dict(orient="index")
    for row in sla.to_dict(orient="records"):
        ticket = tickets_by_id[str(row["ticket_id"])]
        expected_priority = truth_by_ticket[str(row["ticket_id"])]["expected_priority"]
        rule = priority_rules.loc[(expected_priority, ticket["channel_id"])]
        expected_target = int(rule["first_response_minutes"] if row["sla_type"] == "first_response"
                              else rule["resolution_minutes"])
        if int(row["target_minutes"]) != expected_target:
            errors.append(f"SLA {row['sla_id']} target does not reconcile with its expected priority/channel rule.")

    # Complaint cases have a minimum high-priority safety policy.
    for row in tickets.to_dict(orient="records"):
        if intent_names[str(row["intent_id"])] == "complaint" and row["priority"] in {"low", "normal"}:
            if expected_types.get(str(row["ticket_id"])) == "priority_safety_contradiction":
                detected.add("priority_safety_contradiction")
            else:
                errors.append(f"Complaint ticket {row['ticket_id']} is below the required high priority.")

    anomaly_rows = anomalies.to_dict(orient="records")
    for anomaly in anomaly_rows:
        expected_type = str(anomaly["anomaly_type"])
        if expected_type not in ANOMALY_EXPECTATIONS:
            errors.append(f"Unknown anomaly type {expected_type}.")
            continue
        if str(anomaly["expected_validation"]) != ANOMALY_EXPECTATIONS[expected_type]:
            errors.append(f"Anomaly {anomaly['anomaly_id']} has an incorrect expected validation label.")
    if inject_anomalies:
        if len(anomalies) != len(ANOMALY_EXPECTATIONS):
            errors.append(
                f"Expected exactly {len(ANOMALY_EXPECTATIONS)} anomaly rows, found {len(anomalies)}."
            )
        observed_anomaly_types = set(expected_types.values())
        if observed_anomaly_types != set(ANOMALY_EXPECTATIONS):
            errors.append(
                "Anomaly types do not match the five required scenarios: "
                f"{sorted(observed_anomaly_types)}."
            )
        for expected_type in ANOMALY_EXPECTATIONS:
            if expected_type not in detected:
                errors.append(f"Required anomaly scenario was not detected: {expected_type}.")
    elif not anomalies.empty:
        errors.append("Anomaly injection is disabled but anomaly ground-truth rows are present.")

    if not inject_anomalies and (
        ground_truth["anomaly_type"].notna().any()
        or (ground_truth["expected_validation"].astype(str) != "VALID").any()
    ):
        errors.append("Anomaly labels leaked into ground truth while injection is disabled.")
    if "anomaly_type" in events and events["anomaly_type"].notna().any():
        errors.append("Anomaly labels leaked into operational ticket event records.")
    if not inject_anomalies:
        if events["event_type"].astype(str).eq("capacity_violation").any():
            errors.append("A capacity-violation event exists while anomaly injection is disabled.")
        if assignments["decision_reason"].astype(str).str.contains(
            "injected anomaly", case=False
        ).any():
            errors.append("Anomaly injection details leaked into operational assignment records.")

    anomaly_ticket_ids = set(expected_types)
    initial_assignment_by_ticket = {
        str(row["ticket_id"]): row
        for row in assignments[assignments["assignment_number"].astype(int) == 1].to_dict(orient="records")
    }
    resolution_sla_by_ticket = {
        str(row["ticket_id"]): row
        for row in sla[sla["sla_type"] == "resolution"].to_dict(orient="records")
    }
    for anomaly in anomaly_rows:
        truth = truth_by_ticket.get(anomaly["ticket_id"])
        if truth is None:
            errors.append(f"Anomaly {anomaly['anomaly_id']} has no ground-truth row.")
        elif truth["anomaly_type"] != anomaly["anomaly_type"] or truth["expected_validation"] != anomaly["expected_validation"]:
            errors.append(f"Ground truth does not match anomaly {anomaly['anomaly_id']}.")
        try:
            affected = json.loads(str(anomaly["affected_entity_ids"]))
            references = json.loads(str(anomaly["event_references"]))
            if not isinstance(affected, dict) or not isinstance(references, dict):
                raise ValueError("metadata must be JSON objects")
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            errors.append(f"Anomaly {anomaly['anomaly_id']} metadata is invalid: {error}.")
            continue
        if not str(anomaly["expected_behavior"]).strip() or not str(anomaly["observed_behavior"]).strip():
            errors.append(f"Anomaly {anomaly['anomaly_id']} needs expected and observed behavior.")
        if int(anomaly["seed"]) != int(manifest["seed"]):
            errors.append(f"Anomaly {anomaly['anomaly_id']} seed differs from the manifest.")
        reference_values = {
            "ticket_event_ids": set(events["event_id"].astype(str)),
            "assignment_ids": set(assignments["assignment_id"].astype(str)),
            "sla_ids": set(sla["sla_id"].astype(str)),
        }
        for reference_type, valid_ids in reference_values.items():
            referenced_ids = references.get(reference_type, [])
            if not isinstance(referenced_ids, list) or set(map(str, referenced_ids)) - valid_ids:
                errors.append(
                    f"Anomaly {anomaly['anomaly_id']} has invalid {reference_type} references."
                )
    if len(ground_truth) != len(tickets):
        errors.append("Ground truth must contain exactly one row per ticket.")
    if anomaly_ticket_ids - set(ground_truth["ticket_id"].astype(str)):
        errors.append("Anomaly ticket is absent from ground truth.")
    for row in ground_truth.to_dict(orient="records"):
        ticket = tickets_by_id.get(str(row["ticket_id"]))
        if ticket is None:
            errors.append(f"Ground-truth ticket {row['ticket_id']} does not exist.")
            continue
        if row["expected_intent"] != intent_names[str(ticket["intent_id"])]:
            errors.append(f"Ground-truth intent does not match ticket {row['ticket_id']}.")
        if row["expected_queue_id"] != ticket["queue_id"]:
            errors.append(f"Ground-truth queue does not match ticket {row['ticket_id']}.")
        if row["expected_priority"] not in VALID_PRIORITIES:
            errors.append(f"Ground-truth priority is invalid for ticket {row['ticket_id']}.")
        if intent_names[str(ticket["intent_id"])] == "complaint" and row["expected_priority"] not in {"high", "critical"}:
            errors.append(f"Ground-truth safety priority is too low for complaint ticket {row['ticket_id']}.")
        if (
            expected_types.get(str(row["ticket_id"])) != "priority_safety_contradiction"
            and row["expected_priority"] != ticket["priority"]
        ):
            errors.append(f"Ground-truth priority does not match ticket {row['ticket_id']}.")
        initial = initial_assignment_by_ticket.get(str(row["ticket_id"]))
        if initial is not None:
            expected_route = "assigned" if not _is_missing(initial["agent_id"]) else "queue_overflow"
            if row["expected_routing_outcome"] != expected_route:
                errors.append(f"Ground-truth routing outcome does not match ticket {row['ticket_id']}.")
            expected_agent_id = None if _is_missing(initial["agent_id"]) else str(initial["agent_id"])
            actual_agent_id = None if _is_missing(row["expected_agent_id"]) else str(row["expected_agent_id"])
            if actual_agent_id != expected_agent_id:
                errors.append(f"Ground-truth agent does not match routing for ticket {row['ticket_id']}.")
            if row["expected_decision_reason"] != initial["decision_reason"]:
                errors.append(f"Ground-truth decision reason does not match ticket {row['ticket_id']}.")
        if expected_types.get(str(row["ticket_id"])) != "sla_ignores_channel_calendar":
            resolution_sla = resolution_sla_by_ticket.get(str(row["ticket_id"]))
            if resolution_sla is not None:
                expected_sla = "breached" if int(resolution_sla["breached"]) else "met"
                if row["expected_sla_outcome"] != expected_sla:
                    errors.append(f"Ground-truth SLA outcome does not match ticket {row['ticket_id']}.")

    database_path = path / "tickets.sqlite"
    if not database_path.exists():
        errors.append("SQLite export tickets.sqlite is missing.")
        database_counts = {}
    else:
        with closing(sqlite3.connect(database_path)) as connection:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                errors.append(f"SQLite foreign-key check found {len(violations)} violations.")
            csv_tables = {**tables, "anomalies": anomalies, "ground_truth_labels": ground_truth}
            for table_name, frame in csv_tables.items():
                columns = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                if not columns:
                    errors.append(f"SQLite table {table_name} is missing.")
                    continue
                schema_columns = {column[1] for column in columns}
                if table_name == "anomalies":
                    if not schema_columns <= set(frame.columns):
                        errors.append(f"{table_name} CSV is missing SQLite schema columns.")
                        continue
                    frame = frame[list(schema_columns)]
                elif set(frame.columns) != schema_columns:
                    errors.append(f"{table_name} CSV columns do not match the SQLite schema.")
                    continue
                for column in columns:
                    column_name, required = column[1], bool(column[3] or column[5])
                    if required and frame[column_name].isna().any():
                        errors.append(f"{table_name}.{column_name} has null values in a required column.")
                for foreign_key in connection.execute(
                    f'PRAGMA foreign_key_list("{table_name}")'
                ).fetchall():
                    parent_table, source_column, target_column = foreign_key[2], foreign_key[3], foreign_key[4]
                    parent_frame = csv_tables.get(parent_table)
                    if parent_frame is None:
                        errors.append(f"{table_name}.{source_column} references missing table {parent_table}.")
                        continue
                    if not target_column:
                        parent_info = connection.execute(
                            f'PRAGMA table_info("{parent_table}")'
                        ).fetchall()
                        target_column = next(
                            (item[1] for item in parent_info if item[5]), None
                        )
                    if target_column and target_column in parent_frame:
                        valid_values = set(parent_frame[target_column].dropna().astype(str))
                        invalid_values = set(frame[source_column].dropna().astype(str)) - valid_values
                        if invalid_values:
                            errors.append(
                                f"{table_name}.{source_column} has {len(invalid_values)} orphan CSV references."
                            )
            database_counts = {
                name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                for name in [*TABLE_FILES, "anomalies", "ground_truth_labels"]
            }
    for relative_path, expected_hash in manifest.get("checksums_sha256", {}).items():
        file_path = path / relative_path
        if not file_path.exists():
            errors.append(f"Manifest checksum target is missing: {relative_path}.")
        elif hashlib.sha256(file_path.read_bytes()).hexdigest() != expected_hash:
            errors.append(f"Manifest checksum does not match: {relative_path}.")
    for name, expected_count in manifest.get("counts", {}).items():
        frame = tables.get(name)
        if name == "anomalies":
            frame = anomalies
        elif name == "ground_truth_labels":
            frame = ground_truth
        if frame is not None and int(expected_count) != len(frame):
            errors.append(f"Manifest count for {name} does not match the CSV output.")
        if name in database_counts and int(expected_count) != database_counts[name]:
            errors.append(f"Manifest count for {name} does not match the SQLite output.")

    report = {
        "passed": not errors,
        "baseline_checks_passed": not errors,
        "expected_anomalies": [
            {
                "anomaly_id": row["anomaly_id"],
                "ticket_id": row["ticket_id"],
                "anomaly_type": row["anomaly_type"],
                "detected": row["anomaly_type"] in detected,
                "expected_validation": row["expected_validation"],
            }
            for row in anomaly_rows
        ],
        "unexpected_errors": errors,
        "detected_anomaly_types": sorted(detected),
        "counts": {
            **{name: len(frame) for name, frame in tables.items()},
            "anomalies": len(anomalies),
            "ground_truth": len(ground_truth),
        },
        "database_counts": database_counts,
    }
    (path / "validation_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = [
        f"Validation: {'PASSED' if report['passed'] else 'FAILED'}",
        f"Unexpected errors: {len(errors)}",
        f"Expected anomalies detected: {sum(item['detected'] for item in report['expected_anomalies'])}/{len(anomaly_rows)}",
        f"Tickets: {len(tickets):,} | Agents: {len(agents):,} | Messages: {len(tables['messages']):,}",
    ]
    if errors:
        summary.extend(f"- {error}" for error in errors)
    (path / "validation_summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=load_config().output_dir)
    arguments = parser.parse_args()
    raise SystemExit(0 if validate(arguments.data_dir) else 1)
