"""Operational analytics derived from the generated SQLite database."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd

from config import load_config
from src.sla_engine import business_minutes_between


def _query(connection: sqlite3.Connection, sql: str) -> pd.DataFrame:
    return pd.read_sql_query(sql, connection)


def _timestamp(value: Any) -> datetime | None:
    return None if value is None or pd.isna(value) else datetime.fromisoformat(str(value))


def _compliance_by(sla: pd.DataFrame, dimension: str) -> list[dict[str, Any]]:
    if sla.empty:
        return []
    rows = []
    for (group_value, sla_type), group in sla.groupby(
        [dimension, "sla_type"], dropna=False, sort=True
    ):
        rows.append({
            dimension: "Unassigned" if pd.isna(group_value) else str(group_value),
            "sla_type": str(sla_type),
            "cycles": int(len(group)),
            "breaches": int(group["breached"].astype(int).sum()),
            "compliance_rate": float(1 - group["breached"].astype(float).mean()),
            "average_breach_minutes": float(group["breach_minutes"].astype(float).mean()),
        })
    return rows


def _business_duration(
    row: dict[str, Any],
    days_by_calendar: dict[str, set[int]],
    holidays_by_calendar: dict[str, set[date]],
) -> float:
    calendar_id = str(row["calendar_id"])
    return float(business_minutes_between(
        _timestamp(row["started_at"]),
        _timestamp(row["actual_at"]),
        timezone_name=str(row["timezone"]),
        business_start=time.fromisoformat(str(row["business_hours_start"])),
        business_end=time.fromisoformat(str(row["business_hours_end"])),
        weekdays=frozenset(days_by_calendar[calendar_id]),
        holidays=frozenset(holidays_by_calendar[calendar_id]),
    ))


def agent_sla_duration_summary(
    data_dir: str | Path,
    ticket_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return completed SLA business-minute averages by first assigned agent."""
    database = Path(data_dir) / "tickets.sqlite"
    if not database.exists():
        raise FileNotFoundError(f"Generated SQLite database not found: {database}")
    with closing(sqlite3.connect(database)) as connection:
        sla = _query(connection, """
            WITH ranked_cycles AS (
                SELECT s.*, ROW_NUMBER() OVER (
                    PARTITION BY s.ticket_id, s.sla_type, s.started_at
                    ORDER BY s.rowid DESC
                ) AS cycle_rank
                FROM sla_events s
            )
            SELECT s.ticket_id, s.sla_type, s.started_at, s.actual_at, s.calendar_id,
                   c.timezone, c.business_hours_start, c.business_hours_end,
                   a.agent_id, g.agent_name
            FROM ranked_cycles s
            JOIN business_calendars c ON c.calendar_id = s.calendar_id
            LEFT JOIN assignment_events a
              ON a.ticket_id = s.ticket_id AND a.assignment_number = 1
            LEFT JOIN agents g ON g.agent_id = a.agent_id
            WHERE s.cycle_rank = 1 AND s.actual_at IS NOT NULL
        """)
        calendar_days = _query(
            connection, "SELECT calendar_id, weekday FROM calendar_business_days"
        )
        calendar_holidays = _query(
            connection, "SELECT calendar_id, holiday_date FROM calendar_holidays"
        )
    if ticket_ids is not None:
        sla = sla[sla["ticket_id"].isin(ticket_ids)]

    days_by_calendar: dict[str, set[int]] = defaultdict(set)
    holidays_by_calendar: dict[str, set[date]] = defaultdict(set)
    for row in calendar_days.to_dict(orient="records"):
        days_by_calendar[str(row["calendar_id"])].add(int(row["weekday"]))
    for row in calendar_holidays.to_dict(orient="records"):
        holidays_by_calendar[str(row["calendar_id"])].add(
            date.fromisoformat(str(row["holiday_date"]))
        )
    durations = []
    for row in sla.to_dict(orient="records"):
        if pd.isna(row["agent_id"]):
            continue
        durations.append({
            "agent_id": str(row["agent_id"]),
            "agent_name": str(row["agent_name"]),
            "sla_type": str(row["sla_type"]),
            "business_minutes": _business_duration(
                row, days_by_calendar, holidays_by_calendar
            ),
        })
    if not durations:
        return []
    grouped = pd.DataFrame(durations).groupby(
        ["agent_id", "agent_name", "sla_type"], as_index=False
    )["business_minutes"].mean()
    result = []
    for (agent_id, agent_name), group in grouped.groupby(
        ["agent_id", "agent_name"], sort=True
    ):
        averages = group.set_index("sla_type")["business_minutes"]
        result.append({
            "agent_id": str(agent_id),
            "agent_name": str(agent_name),
            "average_first_response_business_minutes": (
                float(averages["first_response"])
                if "first_response" in averages.index else None
            ),
            "average_resolution_business_minutes": (
                float(averages["resolution"])
                if "resolution" in averages.index else None
            ),
        })
    return result


def summarize(data_dir: str | Path | None = None) -> dict[str, Any]:
    directory = Path(load_config().output_dir if data_dir is None else data_dir)
    database = directory / "tickets.sqlite"
    if not database.exists():
        raise FileNotFoundError(f"Generated SQLite database not found: {database}")

    with closing(sqlite3.connect(database)) as connection:
        tickets = _query(connection, """
            SELECT t.ticket_id, t.created_at, t.status, t.priority,
                   i.intent_name AS intent, c.channel_name AS channel,
                   l.language_name AS language, t.queue_id, q.queue_name AS queue
            FROM tickets t
            JOIN intents i ON i.intent_id = t.intent_id
            JOIN channels c ON c.channel_id = t.channel_id
            JOIN languages l ON l.language_id = t.language_id
            JOIN queues q ON q.queue_id = t.queue_id
        """)
        sla = _query(connection, """
            WITH ranked_cycles AS (
                SELECT s.*, ROW_NUMBER() OVER (
                    PARTITION BY s.ticket_id, s.sla_type, s.started_at
                    ORDER BY s.rowid DESC
                ) AS cycle_rank
                FROM sla_events s
            )
            SELECT s.ticket_id, s.sla_type, s.target_minutes, s.started_at, s.due_at,
                   s.actual_at, s.calendar_id, s.breached, s.breach_minutes,
                   c.timezone, c.business_hours_start, c.business_hours_end,
                   t.priority, c2.channel_name AS channel, q.queue_name AS queue,
                   first_assignment.agent_id, g.agent_name
            FROM ranked_cycles s
            JOIN tickets t ON t.ticket_id = s.ticket_id
            JOIN business_calendars c ON c.calendar_id = s.calendar_id
            JOIN channels c2 ON c2.channel_id = t.channel_id
            JOIN queues q ON q.queue_id = t.queue_id
            LEFT JOIN assignment_events first_assignment
              ON first_assignment.ticket_id = s.ticket_id
             AND first_assignment.assignment_number = 1
            LEFT JOIN agents g ON g.agent_id = first_assignment.agent_id
            WHERE s.cycle_rank = 1
        """)
        calendar_days = _query(
            connection, "SELECT calendar_id, weekday FROM calendar_business_days"
        )
        calendar_holidays = _query(
            connection, "SELECT calendar_id, holiday_date FROM calendar_holidays"
        )
        assignments = _query(connection, """
            SELECT a.ticket_id, a.agent_id, a.assigned_at, a.unassigned_at,
                   a.assignment_number, g.max_concurrency
            FROM assignment_events a
            LEFT JOIN agents g ON g.agent_id = a.agent_id
        """)
        agents = _query(connection, """
            SELECT agent_id, agent_name, max_concurrency, current_workload FROM agents
        """)
        events = _query(connection, """
            SELECT ticket_id, event_type, actor_id FROM ticket_events
        """)
        anomalies = _query(connection, """
            SELECT anomaly_id, ticket_id, anomaly_type FROM anomalies
        """)
        queue_backlog = _query(connection, """
            SELECT q.queue_id, q.queue_name, q.capacity,
                   SUM(CASE WHEN t.status IN ('open', 'assigned', 'in_progress', 'pending')
                            THEN 1 ELSE 0 END) AS backlog
            FROM queues q LEFT JOIN tickets t ON t.queue_id = q.queue_id
            GROUP BY q.queue_id, q.queue_name, q.capacity
        """)
        unassigned_tickets = int(connection.execute("""
            SELECT COUNT(*)
            FROM tickets t
            WHERE t.status IN ('open', 'pending')
              AND NOT EXISTS (
                  SELECT 1 FROM assignment_events a
                  WHERE a.ticket_id = t.ticket_id
                    AND a.agent_id IS NOT NULL
                    AND a.unassigned_at IS NULL
              )
        """).fetchone()[0])
        resolved_by_agent = _query(connection, """
            SELECT actor_id AS agent_id, COUNT(DISTINCT ticket_id) AS resolved_ticket_count
            FROM ticket_events
            WHERE event_type = 'resolved' AND actor_id IS NOT NULL
            GROUP BY actor_id
        """)
        transfer_counts = _query(connection, """
            SELECT ticket_id, COUNT(*) AS transfer_count
            FROM assignment_events
            WHERE assignment_status = 'transferred'
            GROUP BY ticket_id
        """)

    ticket_count = len(tickets)
    tickets["created_day"] = pd.to_datetime(tickets["created_at"], utc=True).dt.strftime("%Y-%m-%d")
    statuses = tickets["status"].value_counts().sort_index()
    volume_by_day = tickets.groupby("created_day").size().sort_index()
    volume_by_channel = tickets["channel"].value_counts().sort_index()
    volume_by_intent = tickets["intent"].value_counts().sort_index()
    volume_by_priority = tickets["priority"].value_counts().sort_index()
    volume_by_language = tickets["language"].value_counts().sort_index()

    days_by_calendar: dict[str, set[int]] = defaultdict(set)
    holidays_by_calendar: dict[str, set[date]] = defaultdict(set)
    for row in calendar_days.to_dict(orient="records"):
        days_by_calendar[str(row["calendar_id"])].add(int(row["weekday"]))
    for row in calendar_holidays.to_dict(orient="records"):
        holidays_by_calendar[str(row["calendar_id"])].add(
            date.fromisoformat(str(row["holiday_date"]))
        )

    first_response_durations: list[float] = []
    resolution_durations: list[float] = []
    for row in sla.to_dict(orient="records"):
        if not row["actual_at"]:
            continue
        duration = _business_duration(row, days_by_calendar, holidays_by_calendar)
        if row["sla_type"] == "first_response":
            first_response_durations.append(float(duration))
        else:
            resolution_durations.append(float(duration))

    sla_summary: dict[str, dict[str, Any]] = {}
    for sla_type, group in sla.groupby("sla_type", sort=True):
        breached = group["breached"].astype(int)
        sla_summary[str(sla_type)] = {
            "cycles": int(len(group)),
            "breaches": int(breached.sum()),
            "compliance_rate": float(1 - breached.mean()),
            "breach_rate": float(breached.mean()),
            "average_breach_minutes": float(group["breach_minutes"].astype(float).mean()),
            "average_target_minutes": float(group["target_minutes"].astype(float).mean()),
        }

    load_events: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    assigned_tickets: dict[str, set[str]] = defaultdict(set)
    for row in assignments.to_dict(orient="records"):
        if pd.isna(row["agent_id"]):
            continue
        agent_id = str(row["agent_id"])
        assigned_tickets[agent_id].add(str(row["ticket_id"]))
        started = _timestamp(row["assigned_at"])
        ended = _timestamp(row["unassigned_at"])
        if started is not None:
            load_events[agent_id].append((started, 1))
        if ended is not None:
            load_events[agent_id].append((ended, -1))
    resolved_counts = {
        str(row["agent_id"]): int(row["resolved_ticket_count"])
        for row in resolved_by_agent.to_dict(orient="records")
    }
    workload_rows = []
    for agent in agents.to_dict(orient="records"):
        agent_id = str(agent["agent_id"])
        current = peak = 0
        for _, change in sorted(load_events[agent_id], key=lambda item: (item[0], item[1])):
            current += change
            peak = max(peak, current)
        capacity = int(agent["max_concurrency"])
        workload_rows.append({
            "agent_id": agent_id,
            "agent_name": str(agent["agent_name"]),
            "assigned_ticket_count": len(assigned_tickets[agent_id]),
            "resolved_ticket_count": resolved_counts.get(agent_id, 0),
            "current_workload": int(agent["current_workload"]),
            "peak_concurrent_assignments": peak,
            "max_concurrency": capacity,
            "peak_utilization": peak / capacity,
        })
    workload = pd.DataFrame(workload_rows).sort_values(
        ["peak_utilization", "assigned_ticket_count"], ascending=[False, False]
    )
    assigned_counts = [row["assigned_ticket_count"] for row in workload_rows]
    average_assignments = sum(assigned_counts) / len(assigned_counts) if assigned_counts else 0.0
    workload_imbalance = (
        max(assigned_counts) / average_assignments if average_assignments else 0.0
    )

    transfer_by_ticket = {
        str(row["ticket_id"]): int(row["transfer_count"])
        for row in transfer_counts.to_dict(orient="records")
    }
    transfers_per_ticket = [
        {"ticket_id": str(ticket_id), "transfers": transfer_by_ticket.get(str(ticket_id), 0)}
        for ticket_id in tickets["ticket_id"]
    ]
    transfer_total = sum(transfer_by_ticket.values())
    reopened_tickets = int(
        events.loc[events["event_type"] == "reopened", "ticket_id"].nunique()
    )
    cycles_per_ticket = sla.groupby("ticket_id")["sla_type"].count()
    reopened_sla_cycles = int((cycles_per_ticket > 2).sum())
    current_time = datetime.now(timezone.utc)
    due_times = pd.to_datetime(sla["due_at"], utc=True, errors="coerce")
    actual_missing = sla["actual_at"].isna()
    not_breached = sla["breached"].astype(int).eq(0)
    approaching_mask = (
        actual_missing
        & not_breached
        & due_times.ge(pd.Timestamp(current_time))
        & due_times.le(pd.Timestamp(current_time + timedelta(hours=24)))
    )
    approaching = sla.loc[
        approaching_mask, ["ticket_id", "sla_type", "due_at", "priority", "channel", "queue"]
    ]

    detection_report_path = directory / "validation_report.json"
    detection_report = (
        json.loads(detection_report_path.read_text(encoding="utf-8"))
        if detection_report_path.exists()
        else None
    )
    detected_rows = (
        detection_report.get("expected_anomalies", [])
        if isinstance(detection_report, dict)
        else []
    )
    injected_counts = anomalies["anomaly_type"].value_counts().sort_index()
    detected_counts: dict[str, int] = defaultdict(int)
    for item in detected_rows:
        if item.get("detected"):
            detected_counts[str(item["anomaly_type"])] += 1
    anomaly_types = sorted(set(map(str, injected_counts.index)) | set(detected_counts))
    anomaly_comparison = [
        {
            "anomaly_type": anomaly_type,
            "injected": int(injected_counts.get(anomaly_type, 0)),
            "detected": int(detected_counts.get(anomaly_type, 0)),
        }
        for anomaly_type in anomaly_types
    ]
    agent_sla_durations = agent_sla_duration_summary(directory)

    summary: dict[str, Any] = {
        "ticket_count": int(ticket_count),
        "tickets_by_channel": {str(k): int(v) for k, v in volume_by_channel.items()},
        "tickets_by_intent": {str(k): int(v) for k, v in volume_by_intent.items()},
        "tickets_by_priority": {str(k): int(v) for k, v in volume_by_priority.items()},
        "tickets_by_status": {str(k): int(v) for k, v in statuses.items()},
        "tickets_by_language": {str(k): int(v) for k, v in volume_by_language.items()},
        "ticket_volume_by_day": [
            {"date": str(day), "tickets": int(count)}
            for day, count in volume_by_day.items()
        ],
        "queue_backlog": queue_backlog.fillna(0).to_dict(orient="records"),
        "unassigned_ticket_count": unassigned_tickets,
        "reopened_ticket_count": reopened_tickets,
        "transfer_count": transfer_total,
        "average_transfers_per_ticket": transfer_total / ticket_count if ticket_count else 0.0,
        "transfers_per_ticket": transfers_per_ticket,
        "agent_workload": workload.to_dict(orient="records"),
        "agent_sla_duration_by_agent": agent_sla_durations,
        "workload_imbalance_max_to_mean": float(workload_imbalance),
        "sla": sla_summary,
        "sla_compliance_by_agent": _compliance_by(sla, "agent_name"),
        "sla_compliance_by_queue": _compliance_by(sla, "queue"),
        "sla_compliance_by_channel": _compliance_by(sla, "channel"),
        "sla_compliance_by_priority": _compliance_by(sla, "priority"),
        "average_first_response_business_minutes": (
            sum(first_response_durations) / len(first_response_durations)
            if first_response_durations else None
        ),
        "average_resolution_business_minutes": (
            sum(resolution_durations) / len(resolution_durations)
            if resolution_durations else None
        ),
        "reopened_sla_cycle_ticket_count": reopened_sla_cycles,
        "approaching_sla_deadlines_next_24h": int(len(approaching)),
        "approaching_sla_deadlines": approaching.to_dict(orient="records"),
        "anomaly_ground_truth_vs_detected": {
            "detection_report_available": detection_report is not None,
            "validation_passed": (
                bool(detection_report["passed"])
                if isinstance(detection_report, dict) and "passed" in detection_report
                else None
            ),
            "by_type": anomaly_comparison,
            "precision_recall_f1": None,
            "precision_recall_f1_note": (
                "Not calculated: the validator report only labels expected injected cases "
                "and does not define a complete negative/false-positive population."
            ),
        },
    }
    output = directory / "analytics_summary.json"
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in {"agent_workload", "agent_sla_duration_by_agent",
                                     "transfers_per_ticket",
                                     "approaching_sla_deadlines"}}, indent=2, sort_keys=True))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=load_config().output_dir)
    args = parser.parse_args()
    summarize(args.data_dir)
