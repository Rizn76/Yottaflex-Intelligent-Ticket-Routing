from datetime import date, datetime, time, timedelta

import pytest

from src.database import connect, foreign_key_violations, initialize_database
from src.lifecycle import detect_transfer_loop, transition_ticket, validate_transfer_path
from src.routing import route_ticket, sla_due
from src.sla_engine import add_business_minutes, breach_minutes, calculate_sla


def test_database_schema_initializes_with_foreign_keys(tmp_path):
    database = tmp_path / "routing.db"
    initialize_database(database)
    assert foreign_key_violations(database) == []


def test_database_rejects_orphan_ticket(tmp_path):
    import sqlite3

    database = tmp_path / "routing.db"
    initialize_database(database)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as connection:
            connection.execute(
                "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("TKT-1", "missing", "missing", "missing", "missing", "normal",
                 "missing", "open", "subject", "2026-01-01", "2026-01-01"),
            )


def test_routing_prefers_matching_language_and_available_capacity():
    agents = [
        {"agent_id": "AGT-1", "active": True, "skills": "billing", "languages": "English",
         "shift_start": "09:00", "shift_end": "17:00", "max_concurrency": 2, "current_workload": 1},
        {"agent_id": "AGT-2", "active": True, "skills": "billing", "languages": "Hindi",
         "shift_start": "09:00", "shift_end": "17:00", "max_concurrency": 2, "current_workload": 0},
    ]
    ticket = {"ticket_id": "TKT-1", "intent": "billing", "language": "English",
              "priority": "high", "queue_id": "QUE-1"}
    decision = route_ticket(ticket, agents, now=datetime(2026, 11, 6, 10, 0))
    assert decision["agent_id"] == "AGT-1"
    assert "language" in decision["decision_reason"]


def test_routing_accepts_normalized_skill_and_language_collections():
    ticket = {"ticket_id": "TKT-1", "intent": "billing", "language": "English",
              "priority": "high", "queue_id": "QUE-1"}
    agent = {"agent_id": "AGT-1", "active": True, "skills": ["billing"],
             "languages": ["English"], "shift_start": "09:00", "shift_end": "17:00",
             "max_concurrency": 2}
    decision = route_ticket(ticket, [agent], now=datetime(2026, 11, 6, 10, 0))
    assert decision["agent_id"] == "AGT-1"


def test_routing_rejects_off_shift_or_full_agents_and_reports_overflow():
    ticket = {"ticket_id": "TKT-1", "intent": "billing", "language": "English",
              "priority": "critical", "queue_id": "QUE-1"}
    agents = [{"agent_id": "AGT-1", "active": True, "skills": "billing", "languages": "English",
               "shift_start": "09:00", "shift_end": "17:00", "max_concurrency": 1,
               "current_workload": 1}]
    decision = route_ticket(ticket, agents, now=datetime(2026, 11, 6, 10, 0),
                            queue_backlog=3, queue_capacity=3, overflow_queue_id="QUE-OVERFLOW")
    assert decision["agent_id"] is None
    assert decision["queue_id"] == "QUE-OVERFLOW"
    assert decision["overflow"] is True


def test_routing_returns_unassigned_when_no_agent_is_eligible():
    ticket = {"ticket_id": "TKT-1", "intent": "billing", "language": "English",
              "priority": "normal", "queue_id": "QUE-1"}
    agent = {"agent_id": "AGT-1", "active": False, "skills": "billing", "languages": "English",
             "shift_start": "09:00", "shift_end": "17:00", "max_concurrency": 3}
    decision = route_ticket(ticket, [agent], now=datetime(2026, 11, 6, 10, 0))
    assert decision["agent_id"] is None


@pytest.mark.parametrize(
    "agent, workload, now",
    [
        ({"agent_id": "AGT-1", "active": True, "skills": "billing", "languages": "English",
          "shift_start": "09:00", "shift_end": "17:00", "max_concurrency": 2}, 2,
         datetime(2026, 11, 6, 10, 0)),
        ({"agent_id": "AGT-1", "active": True, "skills": "billing", "languages": "English",
          "shift_start": "09:00", "shift_end": "17:00", "max_concurrency": 2}, 0,
         datetime(2026, 11, 6, 18, 0)),
        ({"agent_id": "AGT-1", "active": True, "skills": "sales", "languages": "English",
          "shift_start": "09:00", "shift_end": "17:00", "max_concurrency": 2}, 0,
         datetime(2026, 11, 6, 10, 0)),
    ],
)
def test_routing_rejects_full_off_shift_or_unskilled_agent(agent, workload, now):
    ticket = {"ticket_id": "TKT-1", "intent": "billing", "language": "English",
              "priority": "normal", "queue_id": "QUE-1"}
    decision = route_ticket(ticket, [agent], now=now, current_workloads={"AGT-1": workload})
    assert decision["agent_id"] is None


def test_critical_routing_does_not_use_language_fallback():
    ticket = {"ticket_id": "TKT-1", "intent": "billing", "language": "English",
              "priority": "critical", "queue_id": "QUE-1"}
    agent = {"agent_id": "AGT-1", "active": True, "skills": "billing", "languages": "Hindi",
             "shift_start": "09:00", "shift_end": "17:00", "max_concurrency": 3}
    decision = route_ticket(ticket, [agent], now=datetime(2026, 11, 6, 10, 0))
    assert decision["agent_id"] is None
    assert "no active" in decision["decision_reason"]


def test_sla_skips_after_hours_weekends_and_holidays():
    started = datetime(2026, 11, 6, 17, 0)  # Friday
    due = add_business_minutes(
        started, 120, timezone_name="Asia/Kolkata",
        holidays={date(2026, 11, 9)},
    )
    assert due == datetime(2026, 11, 10, 10, 0,
                           tzinfo=due.tzinfo)  # Friday hour + Tuesday two hours


def test_sla_rejects_calendar_without_business_weekdays():
    with pytest.raises(ValueError, match="at least one business weekday"):
        add_business_minutes(
            datetime(2026, 11, 6, 10, 0),
            15,
            weekdays=frozenset(),
        )


def test_sla_deadline_and_business_breach_minutes():
    calculation = calculate_sla(
        datetime(2026, 11, 6, 17, 0), 120,
        business_start=time(9), business_end=time(18),
    )
    assert calculation.due_at.date() == date(2026, 11, 9)
    assert breach_minutes(
        calculation.due_at,
        datetime(2026, 11, 9, 10, 0),
        timezone_name="Asia/Kolkata",
    ) == 0


def test_legacy_sla_helper_honors_business_hours_option():
    created = datetime(2026, 11, 6, 17, 30)
    elapsed_due = sla_due(created, "critical")
    business_due = sla_due(created, "critical", business_hours=True)
    assert elapsed_due == created + timedelta(minutes=60)
    assert business_due == datetime(2026, 11, 9, 9, 30, tzinfo=business_due.tzinfo)


def test_lifecycle_rejects_invalid_transitions_and_preserves_reopen_event():
    created = datetime(2026, 11, 6, 9, 0)
    with pytest.raises(ValueError, match="Invalid ticket transition"):
        transition_ticket("created", "resolved", created)
    reopen = transition_ticket(
        "resolved", "open", datetime(2026, 11, 6, 11, 0),
        previous_event_at=datetime(2026, 11, 6, 10, 0),
    )
    assert reopen.event_type == "reopened"
    with pytest.raises(ValueError, match="precedes"):
        transition_ticket("open", "assigned", created,
                          previous_event_at=datetime(2026, 11, 6, 10, 0))


def test_transfer_loop_detection():
    assert detect_transfer_loop(["AGT-1", "AGT-2", "AGT-1"])
    assert not detect_transfer_loop(["AGT-1", "AGT-1"])
    assert not detect_transfer_loop(["AGT-1", "AGT-2"])
    validate_transfer_path(["AGT-1", "AGT-2"], "AGT-3")
    with pytest.raises(ValueError, match="routing loop"):
        validate_transfer_path(["AGT-1", "AGT-2"], "AGT-1")
