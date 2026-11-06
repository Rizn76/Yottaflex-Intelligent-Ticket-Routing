"""Business-calendar SLA deadline and breach calculations."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config import load_config
from src.database import immediate_transaction


@dataclass(frozen=True)
class SLACalculation:
    started_at: datetime
    due_at: datetime
    target_minutes: int


def _localize(value: datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)


def add_business_minutes(
    started_at: datetime,
    minutes: int,
    *,
    timezone_name: str | None = None,
    business_start: time = time(9, 0),
    business_end: time = time(18, 0),
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4}),
    holidays: set[date] | frozenset[date] = frozenset(),
) -> datetime:
    """Add working minutes inside local business windows, skipping days and holidays."""
    if minutes < 0:
        raise ValueError("Business minutes cannot be negative")
    if business_start > business_end:
        raise ValueError("Business-hours start must be before end")
    if not weekdays:
        raise ValueError("Business calendar must include at least one business weekday")

    timezone_name = load_config().timezone_name if timezone_name is None else timezone_name
    zone = ZoneInfo(timezone_name)
    current = _localize(started_at, timezone_name)
    remaining = minutes
    while True:
        day = current.date()
        if day.weekday() not in weekdays or day in holidays:
            current = datetime.combine(day + timedelta(days=1), business_start, zone)
            continue

        opening = datetime.combine(day, business_start, zone)
        closing_day = day + timedelta(days=1) if business_start == business_end else day
        closing = datetime.combine(closing_day, business_end, zone)
        if current < opening:
            current = opening
        elif current >= closing:
            current = datetime.combine(day + timedelta(days=1), business_start, zone)
            continue

        available = int((closing - current).total_seconds() // 60)
        if remaining <= available:
            return current + timedelta(minutes=remaining)
        remaining -= available
        current = datetime.combine(day + timedelta(days=1), business_start, zone)


def calculate_sla(
    started_at: datetime,
    target_minutes: int,
    *,
    timezone_name: str | None = None,
    business_start: time = time(9, 0),
    business_end: time = time(18, 0),
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4}),
    holidays: set[date] | frozenset[date] = frozenset(),
) -> SLACalculation:
    timezone_name = load_config().timezone_name if timezone_name is None else timezone_name
    due_at = add_business_minutes(
        started_at,
        target_minutes,
        timezone_name=timezone_name,
        business_start=business_start,
        business_end=business_end,
        weekdays=weekdays,
        holidays=holidays,
    )
    return SLACalculation(_localize(started_at, timezone_name), due_at, target_minutes)


def business_minutes_between(
    started_at: datetime,
    ended_at: datetime,
    **calendar: object,
) -> int:
    """Return the working minutes in [started_at, ended_at), honoring the same calendar."""
    timezone_value = calendar.get("timezone_name")
    timezone_name = str(
        load_config().timezone_name if timezone_value is None else timezone_value
    )
    zone = ZoneInfo(timezone_name)
    start = _localize(started_at, timezone_name)
    end = _localize(ended_at, timezone_name)
    if end <= start:
        return 0

    business_start = calendar.get("business_start", time(9, 0))
    business_end = calendar.get("business_end", time(18, 0))
    weekdays = calendar.get("weekdays", frozenset({0, 1, 2, 3, 4}))
    holidays = calendar.get("holidays", frozenset())
    if not isinstance(business_start, time) or not isinstance(business_end, time):
        raise TypeError("business_start and business_end must be datetime.time values")
    if business_start > business_end:
        raise ValueError("Business-hours start must be before end")
    if not weekdays:
        raise ValueError("Business calendar must include at least one business weekday")

    count = 0
    day = start.date()
    while day <= end.date():
        if day.weekday() in weekdays and day not in holidays:
            opening = datetime.combine(day, business_start, zone)
            closing_day = day + timedelta(days=1) if business_start == business_end else day
            closing = datetime.combine(closing_day, business_end, zone)
            interval_start = max(start, opening)
            interval_end = min(end, closing)
            if interval_end > interval_start:
                count += int((interval_end - interval_start).total_seconds() // 60)
        day += timedelta(days=1)
    return count


def breach_minutes(
    due_at: datetime,
    actual_at: datetime,
    *,
    business_calendar: bool = True,
    **calendar: object,
) -> int:
    timezone_value = calendar.get("timezone_name")
    timezone_name = str(
        load_config().timezone_name if timezone_value is None else timezone_value
    )
    due = _localize(due_at, timezone_name)
    actual = _localize(actual_at, timezone_name)
    if actual <= due:
        return 0
    if not business_calendar:
        return int((actual - due).total_seconds() // 60)
    return business_minutes_between(due, actual, **calendar)


def _database_datetime(value: datetime, timezone_name: str) -> datetime:
    return _localize(value, timezone_name)


def _load_ticket_sla_context(
    connection: sqlite3.Connection,
    ticket_id: str,
) -> tuple[sqlite3.Row, dict[str, Any]]:
    context = connection.execute(
        """
        SELECT t.ticket_id, t.priority, t.channel_id, t.created_at,
               c.calendar_id, bc.timezone, bc.business_hours_start, bc.business_hours_end
        FROM tickets AS t
        JOIN channels AS c ON c.channel_id = t.channel_id
        JOIN business_calendars AS bc ON bc.calendar_id = c.calendar_id
        WHERE t.ticket_id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if context is None:
        raise LookupError(f"Unknown ticket: {ticket_id}")
    calendar = {
        "timezone_name": str(context["timezone"]),
        "business_start": time.fromisoformat(str(context["business_hours_start"])),
        "business_end": time.fromisoformat(str(context["business_hours_end"])),
        "weekdays": frozenset(
            int(row["weekday"])
            for row in connection.execute(
                "SELECT weekday FROM calendar_business_days WHERE calendar_id = ?",
                (context["calendar_id"],),
            )
        ),
        "holidays": frozenset(
            date.fromisoformat(str(row["holiday_date"]))
            for row in connection.execute(
                "SELECT holiday_date FROM calendar_holidays WHERE calendar_id = ?",
                (context["calendar_id"],),
            )
        ),
    }
    return context, calendar


def _effective_targets(
    connection: sqlite3.Connection,
    context: sqlite3.Row,
    started_at: datetime,
) -> tuple[int, int]:
    rows = connection.execute(
        """
        SELECT first_response_minutes, resolution_minutes, effective_from, effective_to
        FROM priority_rules
        WHERE priority = ? AND channel_id = ?
        ORDER BY julianday(effective_from) DESC
        """,
        (context["priority"], context["channel_id"]),
    ).fetchall()
    for row in rows:
        effective_from = _database_datetime(
            datetime.fromisoformat(str(row["effective_from"])),
            str(context["timezone"]),
        )
        effective_to = (
            None
            if row["effective_to"] is None
            else _database_datetime(
                datetime.fromisoformat(str(row["effective_to"])),
                str(context["timezone"]),
            )
        )
        if effective_from <= started_at and (effective_to is None or started_at < effective_to):
            return int(row["first_response_minutes"]), int(row["resolution_minutes"])
    raise LookupError(
        "No effective SLA priority rule for "
        f"priority={context['priority']!r}, channel={context['channel_id']!r}, "
        f"at={started_at.isoformat()}"
    )


def _sla_event_id(
    connection: sqlite3.Connection,
    ticket_id: str,
    sla_type: str,
) -> str:
    count = int(connection.execute(
        "SELECT COUNT(*) FROM sla_events WHERE ticket_id = ? AND sla_type = ?",
        (ticket_id, sla_type),
    ).fetchone()[0])
    suffix = "FR" if sla_type == "first_response" else ""
    base = f"SLA-{ticket_id}{('-' + suffix) if suffix else ''}"
    return base if count == 0 else f"{base}-{count + 1:03d}"


def _insert_sla_snapshot(
    connection: sqlite3.Connection,
    *,
    ticket_id: str,
    sla_type: str,
    target_minutes: int,
    started_at: datetime,
    due_at: datetime,
    actual_at: datetime | None,
    breached: bool,
    breach_duration: int,
    escalation_at: datetime | None,
    calendar_id: str,
) -> dict[str, Any]:
    sla_id = _sla_event_id(connection, ticket_id, sla_type)
    row = {
        "sla_id": sla_id,
        "ticket_id": ticket_id,
        "sla_type": sla_type,
        "target_minutes": target_minutes,
        "started_at": started_at.isoformat(),
        "due_at": due_at.isoformat(),
        "actual_at": None if actual_at is None else actual_at.isoformat(),
        "breached": int(breached),
        "breach_minutes": max(0, int(breach_duration)),
        "escalation_at": None if escalation_at is None else escalation_at.isoformat(),
        "calendar_id": calendar_id,
    }
    connection.execute(
        """
        INSERT INTO sla_events
            (sla_id, ticket_id, sla_type, target_minutes, started_at, due_at, actual_at,
             breached, breach_minutes, escalation_at, calendar_id)
        VALUES
            (:sla_id, :ticket_id, :sla_type, :target_minutes, :started_at, :due_at,
             :actual_at, :breached, :breach_minutes, :escalation_at, :calendar_id)
        """,
        row,
    )
    return row


def start_sla_cycle(
    connection: sqlite3.Connection,
    ticket_id: str,
    started_at: datetime,
) -> list[dict[str, Any]]:
    """Append first-response and resolution clocks for a new lifecycle cycle."""
    with immediate_transaction(connection):
        context, calendar = _load_ticket_sla_context(connection, ticket_id)
        start = _database_datetime(started_at, calendar["timezone_name"])
        response_target, resolution_target = _effective_targets(connection, context, start)
        snapshots: list[dict[str, Any]] = []
        for sla_type, target in (
            ("first_response", response_target),
            ("resolution", resolution_target),
        ):
            calculation = calculate_sla(start, target, **calendar)
            escalation_at = None
            if context["priority"] == "critical":
                lead_minutes = 5 if sla_type == "first_response" else 15
                escalation_at = max(start, calculation.due_at - timedelta(minutes=lead_minutes))
            snapshots.append(
                _insert_sla_snapshot(
                    connection,
                    ticket_id=ticket_id,
                    sla_type=sla_type,
                    target_minutes=target,
                    started_at=calculation.started_at,
                    due_at=calculation.due_at,
                    actual_at=None,
                    breached=False,
                    breach_duration=0,
                    escalation_at=escalation_at,
                    calendar_id=str(context["calendar_id"]),
                )
            )
        return snapshots


def _latest_sla_event(
    connection: sqlite3.Connection,
    ticket_id: str,
    sla_type: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM sla_events
        WHERE ticket_id = ? AND sla_type = ?
        ORDER BY julianday(started_at) DESC, rowid DESC
        LIMIT 1
        """,
        (ticket_id, sla_type),
    ).fetchone()


def complete_sla_cycle(
    connection: sqlite3.Connection,
    ticket_id: str,
    sla_type: str,
    actual_at: datetime,
) -> dict[str, Any]:
    """Append an immutable completed SLA snapshot; repeated completion is idempotent."""
    if sla_type not in {"first_response", "resolution"}:
        raise ValueError(f"Unknown SLA type: {sla_type}")
    with immediate_transaction(connection):
        current = _latest_sla_event(connection, ticket_id, sla_type)
        if current is None:
            raise LookupError(f"No {sla_type} SLA cycle exists for ticket {ticket_id}")
        context, calendar = _load_ticket_sla_context(connection, ticket_id)
        actual = _database_datetime(actual_at, calendar["timezone_name"])
        started = _database_datetime(
            datetime.fromisoformat(str(current["started_at"])), calendar["timezone_name"]
        )
        if actual < started:
            raise ValueError("SLA actual timestamp precedes its start")
        if current["actual_at"] is not None:
            return dict(current)

        due = _database_datetime(
            datetime.fromisoformat(str(current["due_at"])), calendar["timezone_name"]
        )
        duration = breach_minutes(due, actual, **calendar)
        return _insert_sla_snapshot(
            connection,
            ticket_id=ticket_id,
            sla_type=sla_type,
            target_minutes=int(current["target_minutes"]),
            started_at=started,
            due_at=due,
            actual_at=actual,
            breached=actual > due,
            breach_duration=duration,
            escalation_at=(
                None
                if current["escalation_at"] is None
                else _database_datetime(
                    datetime.fromisoformat(str(current["escalation_at"])),
                    calendar["timezone_name"],
                )
            ),
            calendar_id=str(context["calendar_id"]),
        )


def evaluate_sla_deadlines(
    connection: sqlite3.Connection,
    at: datetime,
    *,
    ticket_id: str | None = None,
) -> list[dict[str, Any]]:
    """Append one breach snapshot when a pending SLA first passes its due time."""
    with immediate_transaction(connection):
        parameters: tuple[Any, ...] = () if ticket_id is None else (ticket_id,)
        ticket_filter = "" if ticket_id is None else "AND s.ticket_id = ?"
        rows = connection.execute(
            f"""
            SELECT s.*
            FROM sla_events AS s
            WHERE s.rowid = (
                SELECT latest.rowid
                FROM sla_events AS latest
                WHERE latest.ticket_id = s.ticket_id
                  AND latest.sla_type = s.sla_type
                ORDER BY julianday(latest.started_at) DESC, latest.rowid DESC
                LIMIT 1
            )
              AND s.actual_at IS NULL
              AND s.breached = 0
              AND julianday(s.due_at) < julianday(?)
              {ticket_filter}
            ORDER BY julianday(s.due_at), s.ticket_id, s.sla_type
            """,
            (_database_datetime(at, load_config().timezone_name).isoformat(), *parameters),
        ).fetchall()
        snapshots: list[dict[str, Any]] = []
        for current in rows:
            context, calendar = _load_ticket_sla_context(connection, str(current["ticket_id"]))
            evaluated = _database_datetime(at, calendar["timezone_name"])
            started = _database_datetime(
                datetime.fromisoformat(str(current["started_at"])), calendar["timezone_name"]
            )
            due = _database_datetime(
                datetime.fromisoformat(str(current["due_at"])), calendar["timezone_name"]
            )
            escalation_at = (
                None
                if current["escalation_at"] is None
                else _database_datetime(
                    datetime.fromisoformat(str(current["escalation_at"])),
                    calendar["timezone_name"],
                )
            )
            snapshots.append(
                _insert_sla_snapshot(
                    connection,
                    ticket_id=str(current["ticket_id"]),
                    sla_type=str(current["sla_type"]),
                    target_minutes=int(current["target_minutes"]),
                    started_at=started,
                    due_at=due,
                    actual_at=None,
                    breached=True,
                    breach_duration=breach_minutes(due, evaluated, **calendar),
                    escalation_at=escalation_at,
                    calendar_id=str(context["calendar_id"]),
                )
            )
        return snapshots
