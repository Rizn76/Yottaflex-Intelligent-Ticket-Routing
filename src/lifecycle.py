"""Ticket state transitions and immutable lifecycle-history helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3
from zoneinfo import ZoneInfo

from config import load_config
from src.database import immediate_transaction
from src.sla_engine import complete_sla_cycle, start_sla_cycle

ALLOWED_TRANSITIONS = {
    "created": {"open"},
    "open": {"assigned", "pending", "resolved"},
    "assigned": {"in_progress", "pending", "open", "resolved"},
    "in_progress": {"pending", "resolved", "open"},
    "pending": {"assigned", "in_progress", "resolved", "open"},
    "resolved": {"closed", "open"},
    "closed": set(),
}


@dataclass(frozen=True)
class LifecycleEvent:
    event_type: str
    from_state: str
    to_state: str
    event_at: datetime
    details: str = ""


def transition_ticket(
    current_state: str,
    next_state: str,
    event_at: datetime,
    previous_event_at: datetime | None = None,
) -> LifecycleEvent:
    """Validate a state transition and return an event without mutating history."""
    if current_state not in ALLOWED_TRANSITIONS:
        raise ValueError(f"Unknown ticket state: {current_state}")
    if next_state not in ALLOWED_TRANSITIONS[current_state]:
        raise ValueError(f"Invalid ticket transition: {current_state} -> {next_state}")
    if previous_event_at is not None and event_at < previous_event_at:
        raise ValueError("Lifecycle event timestamp precedes prior ticket history")
    if current_state == "resolved" and next_state == "open":
        event_type = "reopened"
    else:
        event_type = {
            "open": "opened" if current_state == "created" else "state_changed",
            "assigned": "assigned",
            "in_progress": "work_started",
            "pending": "pending",
            "resolved": "resolved",
            "closed": "closed",
        }[next_state]
    return LifecycleEvent(event_type, current_state, next_state, event_at)


def detect_transfer_loop(agent_history: list[str]) -> bool:
    """A loop occurs when a transfer returns to an agent after visiting another."""
    seen: set[str] = set()
    previous: str | None = None
    for agent_id in agent_history:
        if agent_id != previous and agent_id in seen:
            return True
        seen.add(agent_id)
        previous = agent_id
    return False


def validate_transfer_path(agent_history: list[str], next_agent_id: str) -> None:
    """Reject a proposed transfer that would return to a previously visited agent."""
    if detect_transfer_loop([*agent_history, next_agent_id]):
        raise ValueError(f"Transfer to {next_agent_id} would create a routing loop")


def validate_event_order(events: list[LifecycleEvent]) -> bool:
    """Check that the event stream is chronological and state-contiguous."""
    previous_state: str | None = None
    previous_at: datetime | None = None
    for current in events:
        if previous_at is not None and current.event_at < previous_at:
            return False
        if current.from_state and current.to_state:
            if previous_state is not None and current.from_state != previous_state:
                return False
            previous_state = current.to_state
        previous_at = current.event_at
    return True


def transition_ticket_in_database(
    connection: sqlite3.Connection,
    ticket_id: str,
    next_state: str,
    event_at: datetime,
    *,
    actor_id: str | None = None,
    details: str | None = None,
) -> LifecycleEvent:
    """Persist a validated lifecycle change and release assignment capacity as needed."""
    at = (
        event_at.replace(tzinfo=ZoneInfo(load_config().timezone_name))
        if event_at.tzinfo is None
        else event_at
    )
    with immediate_transaction(connection):
        ticket = connection.execute(
            "SELECT status, updated_at FROM tickets WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
        if ticket is None:
            raise LookupError(f"Unknown ticket: {ticket_id}")
        latest_event = connection.execute(
            """
            SELECT event_at FROM ticket_events WHERE ticket_id = ?
            ORDER BY julianday(event_at) DESC, rowid DESC LIMIT 1
            """,
            (ticket_id,),
        ).fetchone()
        previous_at = datetime.fromisoformat(str(ticket["updated_at"]))
        if previous_at.tzinfo is None:
            previous_at = previous_at.replace(tzinfo=ZoneInfo(load_config().timezone_name))
        if latest_event is not None:
            history_at = datetime.fromisoformat(str(latest_event["event_at"]))
            if history_at.tzinfo is None:
                history_at = history_at.replace(tzinfo=ZoneInfo(load_config().timezone_name))
            previous_at = max(previous_at, history_at)

        event = transition_ticket(str(ticket["status"]), next_state, at, previous_at)
        active_assignment = connection.execute(
            """
            SELECT assignment_id, agent_id
            FROM assignment_events
            WHERE ticket_id = ? AND unassigned_at IS NULL
            ORDER BY assignment_number DESC LIMIT 1
            """,
            (ticket_id,),
        ).fetchone()
        if str(ticket["status"]) in {"assigned", "in_progress"} and (
            active_assignment is None or active_assignment["agent_id"] is None
        ):
            raise ValueError(
                f"Ticket {ticket_id} is {ticket['status']} without a live agent assignment"
            )
        if next_state in {"assigned", "in_progress"} and (
            active_assignment is None or active_assignment["agent_id"] is None
        ):
            raise ValueError(f"Ticket {ticket_id} cannot enter {next_state} without an agent")

        event_number = int(connection.execute(
            "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()[0]) + 1
        event_id = f"EVT-{ticket_id}-LIFECYCLE-{event_number:03d}"
        connection.execute(
            """
            INSERT INTO ticket_events
                (event_id, ticket_id, event_type, from_state, to_state, actor_id, event_at, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                ticket_id,
                event.event_type,
                event.from_state,
                event.to_state,
                actor_id,
                at.isoformat(),
                details or f"Ticket state changed from {event.from_state} to {event.to_state}",
            ),
        )

        released_agent = None
        if active_assignment is not None and next_state not in {"assigned", "in_progress"}:
            connection.execute(
                """
                UPDATE assignment_events
                SET unassigned_at = ?,
                    assignment_status = CASE
                        WHEN agent_id IS NULL THEN 'overflow'
                        ELSE 'unassigned'
                    END
                WHERE assignment_id = ?
                """,
                (at.isoformat(), active_assignment["assignment_id"]),
            )
            released_agent = active_assignment["agent_id"]

        connection.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE ticket_id = ?",
            (next_state, at.isoformat(), ticket_id),
        )
        if released_agent is not None:
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
                (released_agent,),
            ).fetchone()[0])
            connection.execute(
                "UPDATE agents SET current_workload = ? WHERE agent_id = ?",
                (workload, released_agent),
            )

        if next_state == "resolved":
            latest_sla = connection.execute(
                """
                SELECT 1 FROM sla_events
                WHERE ticket_id = ? AND sla_type = 'resolution'
                LIMIT 1
                """,
                (ticket_id,),
            ).fetchone()
            if latest_sla is None:
                start_sla_cycle(connection, ticket_id, at)
            complete_sla_cycle(connection, ticket_id, "resolution", at)
        elif event.event_type == "reopened":
            start_sla_cycle(connection, ticket_id, at)
        return event
