"""Explainable ticket routing and transactional SQLite assignment workflows."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from config import load_config
from src.database import immediate_transaction
from src.lifecycle import transition_ticket
from src.sla_engine import evaluate_sla_deadlines, start_sla_cycle


PRIORITY_RANK = {"critical": 1, "high": 2, "normal": 3, "low": 4}
SLA_MINUTES = {"critical": 60, "high": 240, "normal": 1440, "low": 4320}
CAPACITY_CONSUMING_STATES = frozenset({"assigned", "in_progress"})
QUEUE_BACKLOG_STATES = frozenset({"open"})


class DuplicateAssignmentError(ValueError):
    """Raised when a ticket already has a live agent assignment."""


def _records(agents: pd.DataFrame | Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    if isinstance(agents, pd.DataFrame):
        return agents.to_dict(orient="records")
    return list(agents)


def _values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value}
    if pd.isna(value):
        return set()
    return {item for item in str(value).split("|") if item}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "active"}
    return bool(value)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(load_config().timezone_name))
    return value


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(load_config().timezone_name))
    return parsed


def agent_is_on_shift(agent: Mapping[str, Any], now: datetime) -> bool:
    timezone_name = str(agent.get("timezone") or load_config().timezone_name)
    local_now = _aware(now).astimezone(ZoneInfo(timezone_name))
    start = time.fromisoformat(str(agent.get("shift_start", "00:00")))
    end = time.fromisoformat(str(agent.get("shift_end", "23:59")))
    local_time = local_now.time().replace(tzinfo=None)
    if start <= end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def _evaluate_candidates(
    ticket: Mapping[str, Any],
    agents: Iterable[Mapping[str, Any]],
    now: datetime,
    workloads: Mapping[str, int],
    allow_language_fallback: bool,
) -> tuple[list[tuple[tuple[Any, ...], Mapping[str, Any]]], dict[str, list[str]]]:
    candidates: list[tuple[tuple[Any, ...], Mapping[str, Any]]] = []
    rejections: dict[str, list[str]] = {}
    for agent in agents:
        agent_id = str(agent["agent_id"])
        reasons: list[str] = []
        if not _truthy(agent.get("active", False)):
            reasons.append("inactive")
        if ticket["intent"] not in _values(agent.get("skills")):
            reasons.append("missing_required_skill")
        if not agent_is_on_shift(agent, now):
            reasons.append("off_shift")

        workload = int(workloads.get(agent_id, agent.get("current_workload", 0)) or 0)
        capacity = int(agent.get("max_concurrency", 0) or 0)
        if capacity <= 0 or workload >= capacity:
            reasons.append("at_capacity")

        exact_language = ticket["language"] in _values(agent.get("languages"))
        if not exact_language and not allow_language_fallback:
            reasons.append("language_mismatch_not_allowed")

        if reasons:
            rejections[agent_id] = reasons
            continue

        utilization = workload / capacity
        score = (100.0 if exact_language else 0.0) + (1.0 - utilization) * 10.0
        sort_key = (not exact_language, utilization, workload, agent_id)
        candidates.append(
            (sort_key, {
                "agent_id": agent_id,
                "workload": workload,
                "capacity": capacity,
                "exact_language": exact_language,
                "score": round(score, 6),
            })
        )
    candidates.sort(key=lambda candidate: candidate[0])
    return candidates, rejections


def choose_agent(
    ticket: Mapping[str, Any],
    agents: pd.DataFrame | Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    current_workloads: Mapping[str, int] | None = None,
    allow_language_fallback: bool = True,
) -> tuple[str | None, str]:
    """Choose the least-loaded eligible agent, preferring exact language matches."""
    at = now or datetime.now().astimezone()
    candidates, _ = _evaluate_candidates(
        ticket, _records(agents), at, current_workloads or {}, allow_language_fallback
    )
    if not candidates:
        return None, "no active, on-shift agent with required skill and spare capacity"
    selected = candidates[0][1]
    if selected["exact_language"]:
        return str(selected["agent_id"]), "required skill, language, shift, and capacity matched"
    return (
        str(selected["agent_id"]),
        "language-mismatch fallback; required skill, shift, and capacity matched",
    )


def route_ticket(
    ticket: Mapping[str, Any],
    agents: pd.DataFrame | Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    current_workloads: Mapping[str, int] | None = None,
    queue_backlog: int = 0,
    queue_capacity: int | None = None,
    overflow_queue_id: str | None = None,
    allow_language_fallback: bool | None = None,
) -> dict[str, Any]:
    """Return a deterministic routing decision without mutating input data."""
    at = now or datetime.now().astimezone()
    records = _records(agents)
    workloads = current_workloads or {}
    if allow_language_fallback is None:
        allow_language_fallback = ticket.get("priority") != "critical"

    candidates, rejections = _evaluate_candidates(
        ticket, records, at, workloads, allow_language_fallback
    )
    queue_full = queue_capacity is not None and queue_backlog >= queue_capacity
    selected = candidates[0][1] if candidates else None
    if selected is not None:
        fallback = None
        queue_id = ticket["queue_id"]
        reason = "required skill, language, shift, and capacity matched"
        if not selected["exact_language"]:
            fallback = "language_mismatch"
            reason = "language-mismatch fallback; required skill, shift, and capacity matched"
        overflow = False
    elif queue_full:
        fallback = "queue_capacity_reached"
        queue_id = overflow_queue_id or ticket["queue_id"]
        reason = (
            "queue capacity reached; routed to overflow queue"
            if overflow_queue_id
            else "queue capacity reached; ticket remains unassigned"
        )
        overflow = True
    else:
        fallback = "no_eligible_agent"
        queue_id = ticket["queue_id"]
        reason = "no active, on-shift agent with required skill and spare capacity"
        overflow = False

    selected_id = None if selected is None else str(selected["agent_id"])
    return {
        "ticket_id": ticket["ticket_id"],
        "agent_id": selected_id,
        "queue_id": queue_id,
        "routed_at": _iso(at),
        "priority": ticket["priority"],
        "priority_rank": PRIORITY_RANK.get(ticket["priority"], 99),
        "skill": ticket["intent"],
        "language": ticket["language"],
        "score": None if selected is None else selected["score"],
        "eligible_agent_count": len(candidates),
        "rejections": rejections,
        "fallback": fallback,
        "decision_reason": reason,
        "overflow": overflow,
    }


def _load_database_routing_data(
    connection: sqlite3.Connection,
    ticket_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    ticket_row = connection.execute(
        """
        SELECT t.ticket_id, t.intent_id, i.intent_name AS intent, t.language_id,
               l.language_name AS language, t.priority, t.queue_id, t.status,
               t.created_at, t.updated_at, t.channel_id
        FROM tickets AS t
        JOIN intents AS i ON i.intent_id = t.intent_id
        JOIN languages AS l ON l.language_id = t.language_id
        WHERE t.ticket_id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if ticket_row is None:
        raise LookupError(f"Unknown ticket: {ticket_id}")
    ticket = dict(ticket_row)

    agent_rows = connection.execute(
        """
        SELECT agent_id, timezone, shift_start, shift_end, active,
               max_concurrency, current_workload
        FROM agents
        ORDER BY agent_id
        """
    ).fetchall()
    skill_map: dict[str, set[str]] = {}
    for row in connection.execute(
        """
        SELECT agent_skills.agent_id, skills.skill_name
        FROM agent_skills JOIN skills USING (skill_id)
        ORDER BY agent_skills.agent_id, skills.skill_name
        """
    ):
        skill_map.setdefault(str(row["agent_id"]), set()).add(str(row["skill_name"]))
    language_map: dict[str, set[str]] = {}
    for row in connection.execute(
        """
        SELECT agent_languages.agent_id, languages.language_name
        FROM agent_languages JOIN languages USING (language_id)
        ORDER BY agent_languages.agent_id, languages.language_name
        """
    ):
        language_map.setdefault(str(row["agent_id"]), set()).add(str(row["language_name"]))

    agents = []
    for row in agent_rows:
        agent = dict(row)
        agent_id = str(agent["agent_id"])
        agent["skills"] = skill_map.get(agent_id, set())
        agent["languages"] = language_map.get(agent_id, set())
        agents.append(agent)

    workloads = {str(row["agent_id"]): 0 for row in agent_rows}
    for row in connection.execute(
        """
        SELECT latest.agent_id
        FROM tickets AS t
        JOIN assignment_events AS latest
          ON latest.ticket_id = t.ticket_id
         AND latest.assignment_number = (
             SELECT MAX(history.assignment_number)
             FROM assignment_events AS history
             WHERE history.ticket_id = t.ticket_id
         )
        WHERE t.status IN ('assigned', 'in_progress')
          AND latest.agent_id IS NOT NULL
          AND latest.unassigned_at IS NULL
        """
    ):
        agent_id = str(row["agent_id"])
        workloads[agent_id] = workloads.get(agent_id, 0) + 1
    return ticket, agents, workloads


def _next_assignment_number(connection: sqlite3.Connection, ticket_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(assignment_number), 0) + 1 FROM assignment_events WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    return int(row[0])


def _assert_route_timestamp(
    connection: sqlite3.Connection,
    ticket: Mapping[str, Any],
    routed_at: datetime,
) -> None:
    timestamps = [ticket["created_at"], ticket["updated_at"]]
    latest = connection.execute(
        "SELECT event_at FROM ticket_events WHERE ticket_id = ? ORDER BY julianday(event_at) DESC LIMIT 1",
        (ticket["ticket_id"],),
    ).fetchone()
    if latest is not None:
        timestamps.append(str(latest["event_at"]))
    if any(_parse_datetime(str(value)) > routed_at for value in timestamps):
        raise ValueError("Routing timestamp precedes the ticket's current history")


def route_ticket_in_database(
    connection: sqlite3.Connection,
    ticket_id: str,
    *,
    now: datetime | None = None,
    overflow_queue_id: str | None = None,
    allow_language_fallback: bool | None = None,
) -> dict[str, Any]:
    """Atomically evaluate and persist one routing attempt on an open ticket."""
    routed_at = _aware(now or datetime.now().astimezone())
    with immediate_transaction(connection):
        ticket, agents, workloads = _load_database_routing_data(connection, ticket_id)
        latest_assignment = connection.execute(
            """
            SELECT agent_id, unassigned_at
            FROM assignment_events
            WHERE ticket_id = ?
            ORDER BY assignment_number DESC
            LIMIT 1
            """,
            (ticket_id,),
        ).fetchone()
        if (
            latest_assignment is not None
            and latest_assignment["agent_id"] is not None
            and latest_assignment["unassigned_at"] is None
            and ticket["status"] in CAPACITY_CONSUMING_STATES
        ):
            raise DuplicateAssignmentError(f"Ticket {ticket_id} already has a live assignment")
        if ticket["status"] not in {"open", "pending"}:
            raise ValueError(f"Ticket {ticket_id} is not routable from state {ticket['status']!r}")

        queue_row = connection.execute(
            "SELECT capacity, active FROM queues WHERE queue_id = ?",
            (ticket["queue_id"],),
        ).fetchone()
        if queue_row is None or not int(queue_row["active"]):
            raise ValueError(f"Ticket {ticket_id} is assigned to an inactive queue")
        backlog = int(connection.execute(
            "SELECT COUNT(*) FROM tickets WHERE queue_id = ? AND status = 'open'",
            (ticket["queue_id"],),
        ).fetchone()[0])

        decision = route_ticket(
            ticket,
            agents,
            now=routed_at,
            current_workloads=workloads,
            queue_backlog=backlog,
            queue_capacity=int(queue_row["capacity"]),
            overflow_queue_id=overflow_queue_id,
            allow_language_fallback=allow_language_fallback,
        )
        if int(connection.execute(
            "SELECT COUNT(*) FROM sla_events WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()[0]) == 0:
            start_sla_cycle(connection, ticket_id, _parse_datetime(str(ticket["created_at"])))
        if decision["queue_id"] != ticket["queue_id"]:
            overflow = connection.execute(
                "SELECT active FROM queues WHERE queue_id = ?",
                (decision["queue_id"],),
            ).fetchone()
            if overflow is None or not int(overflow["active"]):
                raise ValueError(f"Overflow queue is missing or inactive: {decision['queue_id']}")

        _assert_route_timestamp(connection, ticket, routed_at)
        if (
            latest_assignment is not None
            and latest_assignment["agent_id"] is None
            and latest_assignment["unassigned_at"] is None
        ):
            connection.execute(
                "UPDATE assignment_events SET unassigned_at = ? WHERE assignment_id = ?",
                (_iso(routed_at), latest_assignment["assignment_id"]),
            )
        assignment_number = _next_assignment_number(connection, ticket_id)
        assignment_id = f"ASN-{ticket_id}-{assignment_number:03d}"
        persisted_explanation = json.dumps(decision, sort_keys=True, separators=(",", ":"))
        connection.execute(
            """
            INSERT INTO assignment_events
                (assignment_id, ticket_id, agent_id, queue_id, assigned_at, unassigned_at,
                 assignment_status, decision_reason, assignment_number)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                assignment_id,
                ticket_id,
                decision["agent_id"],
                decision["queue_id"],
                _iso(routed_at),
                "assigned" if decision["agent_id"] else "overflow",
                persisted_explanation,
                assignment_number,
            ),
        )

        if decision["agent_id"] is not None:
            event_id = f"EVT-{ticket_id}-ROUTE-{assignment_number:03d}"
            lifecycle_event = transition_ticket(
                str(ticket["status"]),
                "assigned",
                routed_at,
                _parse_datetime(str(ticket["updated_at"])),
            )
            connection.execute(
                """
                INSERT INTO ticket_events
                    (event_id, ticket_id, event_type, from_state, to_state, event_at, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    ticket_id,
                    lifecycle_event.event_type,
                    lifecycle_event.from_state,
                    lifecycle_event.to_state,
                    _iso(routed_at),
                    decision["decision_reason"],
                ),
            )
            connection.execute(
                "UPDATE tickets SET queue_id = ?, status = 'assigned', updated_at = ? WHERE ticket_id = ?",
                (decision["queue_id"], _iso(routed_at), ticket_id),
            )
            workload = workloads.get(str(decision["agent_id"]), 0) + 1
            connection.execute(
                "UPDATE agents SET current_workload = ? WHERE agent_id = ?",
                (workload, decision["agent_id"]),
            )
        else:
            connection.execute(
                "UPDATE tickets SET queue_id = ?, updated_at = ? WHERE ticket_id = ?",
                (decision["queue_id"], _iso(routed_at), ticket_id),
            )

        return {**decision, "assignment_id": assignment_id, "assignment_number": assignment_number}


def route_next_ticket(
    connection: sqlite3.Connection,
    queue_id: str,
    *,
    now: datetime | None = None,
    overflow_queue_id: str | None = None,
) -> dict[str, Any] | None:
    """Route the highest-priority open ticket, using FIFO and ticket ID as stable ties."""
    with immediate_transaction(connection):
        queue = connection.execute(
            "SELECT active FROM queues WHERE queue_id = ?", (queue_id,)
        ).fetchone()
        if queue is None:
            raise LookupError(f"Unknown queue: {queue_id}")
        if not int(queue["active"]):
            raise ValueError(f"Queue is inactive: {queue_id}")
        row = connection.execute(
            """
            SELECT ticket_id
            FROM tickets
            WHERE queue_id = ? AND status = 'open'
            ORDER BY CASE priority
                WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                WHEN 'normal' THEN 3 WHEN 'low' THEN 4
            END, julianday(created_at), ticket_id
            LIMIT 1
            """,
            (queue_id,),
        ).fetchone()
        if row is None:
            return None
        return route_ticket_in_database(
            connection,
            str(row["ticket_id"]),
            now=now,
            overflow_queue_id=overflow_queue_id,
        )


def process_sla_escalations(
    connection: sqlite3.Connection,
    at: datetime,
    *,
    escalation_queue_id: str | None = None,
) -> list[dict[str, Any]]:
    """Record due escalations once, optionally transferring tickets to a queue."""
    escalated: list[dict[str, Any]] = []
    with immediate_transaction(connection):
        evaluate_sla_deadlines(connection, at)
        escalation_capacity = None
        if escalation_queue_id is not None:
            queue = connection.execute(
                "SELECT active, capacity FROM queues WHERE queue_id = ?",
                (escalation_queue_id,),
            ).fetchone()
            if queue is None or not int(queue["active"]):
                raise ValueError(f"Escalation queue is missing or inactive: {escalation_queue_id}")
            escalation_capacity = int(queue["capacity"])

        pending = connection.execute(
            """
            SELECT s.ticket_id, s.sla_type, s.started_at, s.escalation_at,
                   t.status, t.updated_at, t.queue_id
            FROM sla_events AS s
            JOIN tickets AS t ON t.ticket_id = s.ticket_id
            WHERE s.rowid = (
                SELECT latest.rowid
                FROM sla_events AS latest
                WHERE latest.ticket_id = s.ticket_id
                  AND latest.sla_type = s.sla_type
                ORDER BY julianday(latest.started_at) DESC, latest.rowid DESC
                LIMIT 1
            )
              AND s.actual_at IS NULL
              AND s.escalation_at IS NOT NULL
              AND julianday(s.escalation_at) <= julianday(?)
              AND t.status NOT IN ('resolved', 'closed')
            ORDER BY julianday(s.escalation_at), s.ticket_id, s.sla_type
            """,
            (_iso(_aware(at)),),
        ).fetchall()
        for item in pending:
            ticket_id = str(item["ticket_id"])
            cycle_key = f"SLA escalation: {ticket_id}:{item['sla_type']}:{item['started_at']}"
            exists = connection.execute(
                """
                SELECT 1 FROM ticket_events
                WHERE ticket_id = ? AND event_type = 'escalated' AND details = ?
                """,
                (ticket_id, cycle_key),
            ).fetchone()
            if exists is not None:
                continue

            effective_at = _aware(at)
            latest = connection.execute(
                """
                SELECT event_at FROM ticket_events WHERE ticket_id = ?
                ORDER BY julianday(event_at) DESC, rowid DESC LIMIT 1
                """,
                (ticket_id,),
            ).fetchone()
            previous_at = max(
                _parse_datetime(str(item["updated_at"])),
                _parse_datetime(str(latest["event_at"])) if latest is not None
                else _parse_datetime(str(item["updated_at"])),
            )
            if effective_at < previous_at:
                raise ValueError(f"Escalation timestamp precedes ticket history: {ticket_id}")

            move_to_escalation_queue = False
            if escalation_queue_id is not None:
                backlog = int(connection.execute(
                    "SELECT COUNT(*) FROM tickets WHERE queue_id = ? AND status = 'open'",
                    (escalation_queue_id,),
                ).fetchone()[0])
                projected_backlog = backlog + int(
                    not (
                        item["status"] == "open"
                        and item["queue_id"] == escalation_queue_id
                    )
                )
                move_to_escalation_queue = projected_backlog <= int(escalation_capacity or 0)

            if move_to_escalation_queue and escalation_queue_id is not None:
                active_assignment = connection.execute(
                    """
                    SELECT assignment_id, agent_id
                    FROM assignment_events
                    WHERE ticket_id = ? AND unassigned_at IS NULL
                    ORDER BY assignment_number DESC LIMIT 1
                    """,
                    (ticket_id,),
                ).fetchone()
                old_status = str(item["status"])
                if active_assignment is not None:
                    connection.execute(
                        """
                        UPDATE assignment_events
                        SET unassigned_at = ?,
                            assignment_status = CASE
                                WHEN agent_id IS NULL THEN 'overflow'
                                ELSE 'transferred'
                            END
                        WHERE assignment_id = ?
                        """,
                        (_iso(effective_at), active_assignment["assignment_id"]),
                    )
                if old_status in {"assigned", "in_progress", "pending"}:
                    lifecycle_event = transition_ticket(
                        old_status,
                        "open",
                        effective_at,
                        previous_at,
                    )
                    event_number = int(connection.execute(
                        "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = ?",
                        (ticket_id,),
                    ).fetchone()[0]) + 1
                    connection.execute(
                        """
                        INSERT INTO ticket_events
                            (event_id, ticket_id, event_type, from_state, to_state, event_at, details)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"EVT-{ticket_id}-ESCALATION-STATE-{event_number:03d}",
                            ticket_id,
                            lifecycle_event.event_type,
                            lifecycle_event.from_state,
                            lifecycle_event.to_state,
                            _iso(effective_at),
                            "Ticket moved to the escalation queue",
                        ),
                    )
                    updated_status = "open"
                else:
                    updated_status = old_status
                assignment_number = _next_assignment_number(connection, ticket_id)
                connection.execute(
                    """
                    INSERT INTO assignment_events
                        (assignment_id, ticket_id, agent_id, queue_id, assigned_at, unassigned_at,
                         assignment_status, decision_reason, assignment_number)
                    VALUES (?, ?, NULL, ?, ?, NULL, 'overflow', ?, ?)
                    """,
                    (
                        f"ASN-{ticket_id}-{assignment_number:03d}",
                        ticket_id,
                        escalation_queue_id,
                        _iso(effective_at),
                        cycle_key,
                        assignment_number,
                    ),
                )
                connection.execute(
                    "UPDATE tickets SET queue_id = ?, status = ?, updated_at = ? WHERE ticket_id = ?",
                    (escalation_queue_id, updated_status, _iso(effective_at), ticket_id),
                )
                if active_assignment is not None and active_assignment["agent_id"] is not None:
                    agent_id = str(active_assignment["agent_id"])
                    workload = int(connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM tickets AS t
                        JOIN assignment_events AS latest
                          ON latest.ticket_id = t.ticket_id
                         AND latest.assignment_number = (
                             SELECT MAX(history.assignment_number)
                             FROM assignment_events AS history
                             WHERE history.ticket_id = t.ticket_id
                         )
                        WHERE latest.agent_id = ?
                          AND latest.unassigned_at IS NULL
                          AND t.status IN ('assigned', 'in_progress')
                        """,
                        (agent_id,),
                    ).fetchone()[0])
                    connection.execute(
                        "UPDATE agents SET current_workload = ? WHERE agent_id = ?",
                        (workload, agent_id),
                    )

            event_number = int(connection.execute(
                "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()[0]) + 1
            connection.execute(
                """
                INSERT INTO ticket_events
                    (event_id, ticket_id, event_type, event_at, details)
                VALUES (?, ?, 'escalated', ?, ?)
                """,
                (
                    f"EVT-{ticket_id}-ESCALATED-{event_number:03d}",
                    ticket_id,
                    _iso(effective_at),
                    cycle_key,
                ),
            )
            escalated.append({
                "ticket_id": ticket_id,
                "sla_type": str(item["sla_type"]),
                "escalated_at": _iso(effective_at),
                "queue_id": (
                    escalation_queue_id
                    if move_to_escalation_queue and escalation_queue_id is not None
                    else item["queue_id"]
                ),
                "fallback": (
                    "escalation_queue_at_capacity"
                    if escalation_queue_id is not None and not move_to_escalation_queue
                    else None
                ),
            })
    return escalated


def sla_due(created_at, priority, business_hours=False):
    """Return a legacy priority deadline; use the configured calendar if requested."""
    from src.sla_engine import calculate_sla

    minutes = SLA_MINUTES[priority]
    if business_hours:
        return calculate_sla(created_at, minutes).due_at
    return pd.Timestamp(created_at) + pd.Timedelta(minutes=minutes)
