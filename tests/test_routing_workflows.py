from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.database import connect, initialize_database
from src.lifecycle import transition_ticket_in_database
from src.routing import (
    DuplicateAssignmentError,
    process_sla_escalations,
    route_next_ticket,
    route_ticket_in_database,
)
from src.sla_engine import complete_sla_cycle, evaluate_sla_deadlines


LOCAL = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 11, 6, 10, 0, tzinfo=LOCAL)


def _workflow_database(path, *, queue_capacity=10, agent_capacity=2):
    initialize_database(path)
    with closing(connect(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO business_calendars
                (calendar_id, calendar_name, timezone, business_hours_start, business_hours_end)
            VALUES ('CAL-01', 'Weekday Support', 'Asia/Kolkata', '09:00', '18:00')
            """
        )
        connection.executemany(
            "INSERT INTO calendar_business_days (calendar_id, weekday) VALUES ('CAL-01', ?)",
            [(day,) for day in range(5)],
        )
        connection.execute(
            "INSERT INTO languages (language_id, language_name) VALUES ('LANG-01', 'English')"
        )
        connection.execute(
            """
            INSERT INTO channels
                (channel_id, channel_name, business_hours_start, business_hours_end, timezone, calendar_id)
            VALUES ('CH-01', 'email', '09:00', '18:00', 'Asia/Kolkata', 'CAL-01')
            """
        )
        connection.execute(
            "INSERT INTO intents (intent_id, intent_name, default_priority) VALUES ('INT-01', 'Billing', 'normal')"
        )
        connection.executemany(
            """
            INSERT INTO priority_rules
                (priority_rule_id, priority, channel_id, first_response_minutes,
                 resolution_minutes, effective_from, effective_to)
            VALUES (?, ?, 'CH-01', ?, ?, '2026-08-01T00:00:00+05:30', NULL)
            """,
            [
                (f"RULE-{priority}", priority, first_response, resolution)
                for priority, first_response, resolution in (
                    ("critical", 5, 60),
                    ("high", 15, 240),
                    ("normal", 60, 1440),
                    ("low", 240, 4320),
                )
            ],
        )
        connection.execute(
            "INSERT INTO customers (customer_id, customer_segment, country, created_at) "
            "VALUES ('CUS-01', 'consumer', 'IN', ?)",
            (START.isoformat(),),
        )
        connection.execute(
            "INSERT INTO skills (skill_id, skill_name) VALUES ('SK-01', 'Billing')"
        )
        connection.executemany(
            """
            INSERT INTO agents
                (agent_id, agent_name, timezone, shift_start, shift_end,
                 active, max_concurrency, current_workload)
            VALUES (?, ?, 'Asia/Kolkata', '00:00', '23:59', 1, ?, 0)
            """,
            [("AGT-01", "Agent One", agent_capacity), ("AGT-02", "Agent Two", agent_capacity)],
        )
        connection.executemany(
            "INSERT INTO agent_skills (agent_id, skill_id) VALUES (?, 'SK-01')",
            [("AGT-01",), ("AGT-02",)],
        )
        connection.executemany(
            "INSERT INTO agent_languages (agent_id, language_id) VALUES (?, 'LANG-01')",
            [("AGT-01",), ("AGT-02",)],
        )
        connection.executemany(
            """
            INSERT INTO queues (queue_id, queue_name, intent_id, capacity, active)
            VALUES (?, ?, 'INT-01', ?, 1)
            """,
            [
                ("QUE-01", "Billing Queue", queue_capacity),
                ("QUE-ESC", "Escalation Queue", queue_capacity),
            ],
        )
        connection.execute(
            """
            INSERT INTO tickets
                (ticket_id, customer_id, channel_id, intent_id, language_id, priority,
                 queue_id, status, subject, created_at, updated_at)
            VALUES ('TKT-01', 'CUS-01', 'CH-01', 'INT-01', 'LANG-01', 'high',
                    'QUE-01', 'open', 'Billing question', ?, ?)
            """,
            (START.isoformat(), START.isoformat()),
        )
        connection.executemany(
            """
            INSERT INTO ticket_events
                (event_id, ticket_id, event_type, from_state, to_state, event_at, details)
            VALUES (?, 'TKT-01', ?, ?, ?, ?, ?)
            """,
            [
                ("EVT-TKT-01-001", "created", None, "created", START.isoformat(), "Ticket created"),
                ("EVT-TKT-01-002", "opened", "created", "open", START.isoformat(), "Ticket opened"),
            ],
        )
    return path


def _add_ticket(connection, ticket_id, priority, created_at=START):
    connection.execute(
        """
        INSERT INTO tickets
            (ticket_id, customer_id, channel_id, intent_id, language_id, priority,
             queue_id, status, subject, created_at, updated_at)
        VALUES (?, 'CUS-01', 'CH-01', 'INT-01', 'LANG-01', ?, 'QUE-01',
                'open', 'Billing question', ?, ?)
        """,
        (ticket_id, priority, created_at.isoformat(), created_at.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO ticket_events
            (event_id, ticket_id, event_type, from_state, to_state, event_at, details)
        VALUES (?, ?, 'created', NULL, 'created', ?, 'Ticket created')
        """,
        (f"EVT-{ticket_id}-001", ticket_id, created_at.isoformat()),
    )


def test_routing_persists_explanation_assignment_capacity_and_sla(tmp_path):
    database = _workflow_database(tmp_path / "workflow.db")
    with closing(connect(database)) as connection:
        connection.execute("UPDATE agents SET current_workload = max_concurrency")
        decision = route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))
        assert decision["agent_id"] == "AGT-01"
        assert decision["priority"] == "high"
        assert decision["skill"] == "Billing"
        assert decision["language"] == "English"
        assert decision["score"] == 110.0
        assert decision["eligible_agent_count"] == 2
        assert decision["fallback"] is None
        with pytest.raises(DuplicateAssignmentError):
            route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=2))

        assignment = connection.execute(
            "SELECT * FROM assignment_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()
        assert assignment["agent_id"] == "AGT-01"
        assert "eligible_agent_count" in assignment["decision_reason"]
        assert connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = 'TKT-01'"
        ).fetchone()["status"] == "assigned"
        assert connection.execute(
            "SELECT current_workload FROM agents WHERE agent_id = 'AGT-01'"
        ).fetchone()["current_workload"] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sla_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == 2


def test_capacity_fallback_is_explained_and_can_be_retried(tmp_path):
    database = _workflow_database(tmp_path / "capacity.db", agent_capacity=1)
    with closing(connect(database)) as connection:
        first = route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))
        assert first["agent_id"] == "AGT-01"
        connection.execute("UPDATE agents SET active = 0 WHERE agent_id = 'AGT-02'")
        _add_ticket(connection, "TKT-02", "normal", START + timedelta(minutes=2))

        waiting = route_ticket_in_database(
            connection, "TKT-02", now=START + timedelta(minutes=3)
        )
        assert waiting["agent_id"] is None
        assert waiting["fallback"] == "no_eligible_agent"
        assert waiting["rejections"] == {
            "AGT-01": ["at_capacity"],
            "AGT-02": ["inactive"],
        }
        assert connection.execute(
            "SELECT assignment_status FROM assignment_events WHERE ticket_id = 'TKT-02'"
        ).fetchone()[0] == "overflow"
        with pytest.raises(DuplicateAssignmentError):
            route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=4))


def test_unassigned_overflow_history_can_close_on_lifecycle_change(tmp_path):
    database = _workflow_database(tmp_path / "unassigned.db", agent_capacity=1)
    with closing(connect(database)) as connection:
        connection.execute("UPDATE agents SET active = 0")
        route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))
        transition_ticket_in_database(
            connection, "TKT-01", "resolved", START + timedelta(minutes=5)
        )
        assignment = connection.execute(
            "SELECT assignment_status, unassigned_at FROM assignment_events "
            "WHERE ticket_id = 'TKT-01'"
        ).fetchone()
        assert assignment["assignment_status"] == "overflow"
        assert assignment["unassigned_at"] is not None
        assert connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == "resolved"


def test_dispatch_selects_highest_priority_then_fifo(tmp_path):
    database = _workflow_database(tmp_path / "priority.db")
    with closing(connect(database)) as connection:
        _add_ticket(connection, "TKT-02", "critical", START + timedelta(minutes=1))
        decision = route_next_ticket(connection, "QUE-01", now=START + timedelta(minutes=2))
        assert decision is not None
        assert decision["ticket_id"] == "TKT-02"
        assert decision["priority"] == "critical"


def test_full_queue_uses_configured_overflow_without_exceeding_capacity(tmp_path):
    database = _workflow_database(tmp_path / "overflow.db", queue_capacity=1)
    with closing(connect(database)) as connection:
        connection.execute("UPDATE agents SET active = 0")
        decision = route_ticket_in_database(
            connection,
            "TKT-01",
            now=START + timedelta(minutes=1),
            overflow_queue_id="QUE-ESC",
        )
        assert decision["agent_id"] is None
        assert decision["queue_id"] == "QUE-ESC"
        assert decision["fallback"] == "queue_capacity_reached"
        assert connection.execute(
            "SELECT queue_id FROM tickets WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == "QUE-ESC"
        assert connection.execute(
            "SELECT assignment_status FROM assignment_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == "overflow"


def test_sla_cycles_are_append_only_and_reopen_starts_new_clocks(tmp_path):
    database = _workflow_database(tmp_path / "sla.db")
    with closing(connect(database)) as connection:
        route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))
        first_response = complete_sla_cycle(
            connection, "TKT-01", "first_response", START + timedelta(minutes=5)
        )
        assert first_response["actual_at"] is not None
        assert first_response["breached"] == 0
        initial_sla_count = connection.execute(
            "SELECT COUNT(*) FROM sla_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0]

        transition_ticket_in_database(
            connection, "TKT-01", "in_progress", START + timedelta(minutes=6)
        )
        transition_ticket_in_database(
            connection, "TKT-01", "resolved", START + timedelta(minutes=20)
        )
        transition_ticket_in_database(
            connection, "TKT-01", "open", START + timedelta(minutes=30)
        )
        resolution_starts = connection.execute(
            """
            SELECT COUNT(DISTINCT started_at) FROM sla_events
            WHERE ticket_id = 'TKT-01' AND sla_type = 'resolution'
            """
        ).fetchone()[0]
        assert resolution_starts == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM sla_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] > initial_sla_count
        assert connection.execute(
            "SELECT current_workload FROM agents WHERE agent_id = 'AGT-01'"
        ).fetchone()[0] == 0


def test_sla_deadlines_and_escalation_are_idempotent(tmp_path):
    database = _workflow_database(tmp_path / "escalation.db")
    with closing(connect(database)) as connection:
        connection.execute("UPDATE tickets SET priority = 'critical' WHERE ticket_id = 'TKT-01'")
        route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))

        overdue = evaluate_sla_deadlines(connection, START + timedelta(minutes=6))
        assert any(row["sla_type"] == "first_response" for row in overdue)
        escalated = process_sla_escalations(
            connection,
            START + timedelta(minutes=6),
            escalation_queue_id="QUE-ESC",
        )
        assert len(escalated) == 1
        assert escalated[0]["queue_id"] == "QUE-ESC"
        assert process_sla_escalations(
            connection,
            START + timedelta(minutes=7),
            escalation_queue_id="QUE-ESC",
        ) == []
        assert connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == "open"
        assert connection.execute(
            "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = 'TKT-01' "
            "AND event_type = 'escalated'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT current_workload FROM agents WHERE agent_id = 'AGT-01'"
        ).fetchone()[0] == 0


def test_escalation_does_not_exceed_escalation_queue_capacity(tmp_path):
    database = _workflow_database(tmp_path / "escalation-capacity.db", queue_capacity=1)
    with closing(connect(database)) as connection:
        connection.execute("UPDATE tickets SET priority = 'critical' WHERE ticket_id = 'TKT-01'")
        route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))
        _add_ticket(connection, "TKT-02", "normal", START + timedelta(minutes=2))
        connection.execute(
            "UPDATE tickets SET queue_id = 'QUE-ESC' WHERE ticket_id = 'TKT-02'"
        )

        result = process_sla_escalations(
            connection,
            START + timedelta(minutes=6),
            escalation_queue_id="QUE-ESC",
        )
        assert len(result) == 1
        assert result[0]["fallback"] == "escalation_queue_at_capacity"
        assert result[0]["queue_id"] == "QUE-01"
        assert connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == "assigned"
        assert connection.execute(
            "SELECT current_workload FROM agents WHERE agent_id = 'AGT-01'"
        ).fetchone()[0] == 1


def test_failed_routing_rolls_back_without_partial_records(tmp_path):
    database = _workflow_database(tmp_path / "rollback.db", queue_capacity=1)
    with closing(connect(database)) as connection:
        connection.execute("UPDATE agents SET active = 0")
        connection.execute("UPDATE queues SET active = 0 WHERE queue_id = 'QUE-ESC'")
        with pytest.raises(ValueError, match="Overflow queue"):
            route_ticket_in_database(
                connection,
                "TKT-01",
                now=START + timedelta(minutes=1),
                overflow_queue_id="QUE-ESC",
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM assignment_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sla_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == 0


def test_lifecycle_events_are_specific_and_resolution_closure_release_capacity(tmp_path):
    database = _workflow_database(tmp_path / "lifecycle.db")
    with closing(connect(database)) as connection:
        route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))
        initial_event_ids = {
            row["event_id"]
            for row in connection.execute(
                "SELECT event_id FROM ticket_events WHERE ticket_id = 'TKT-01'"
            )
        }
        transition_ticket_in_database(
            connection,
            "TKT-01",
            "in_progress",
            START + timedelta(minutes=2),
            actor_id="AGT-01",
            details="Agent began handling the request",
        )
        transition_ticket_in_database(
            connection,
            "TKT-01",
            "resolved",
            START + timedelta(minutes=10),
            actor_id="AGT-01",
            details="Resolved with customer guidance",
        )
        assignment = connection.execute(
            "SELECT assignment_status, unassigned_at FROM assignment_events "
            "WHERE ticket_id = 'TKT-01'"
        ).fetchone()
        assert assignment["assignment_status"] == "unassigned"
        assert assignment["unassigned_at"] == (START + timedelta(minutes=10)).isoformat()
        assert connection.execute(
            "SELECT current_workload FROM agents WHERE agent_id = 'AGT-01'"
        ).fetchone()[0] == 0

        transition_ticket_in_database(
            connection,
            "TKT-01",
            "closed",
            START + timedelta(minutes=11),
            actor_id="AGT-01",
            details="Closed after resolution",
        )
        events = connection.execute(
            "SELECT event_id, event_type, from_state, to_state, actor_id, details "
            "FROM ticket_events WHERE ticket_id = 'TKT-01' ORDER BY julianday(event_at), rowid"
        ).fetchall()
        assert initial_event_ids <= {row["event_id"] for row in events}
        assert [row["event_type"] for row in events[-3:]] == [
            "work_started",
            "resolved",
            "closed",
        ]
        assert events[-3]["actor_id"] == "AGT-01"
        assert events[-2]["details"] == "Resolved with customer guidance"
        assert connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == "closed"
        assert connection.execute(
            "SELECT current_workload FROM agents WHERE agent_id = 'AGT-01'"
        ).fetchone()[0] == 0

        event_count = len(events)
        with pytest.raises(ValueError, match="Invalid ticket transition"):
            transition_ticket_in_database(
                connection, "TKT-01", "closed", START + timedelta(minutes=12)
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == event_count


def test_failed_lifecycle_transition_rolls_back_without_changing_assignment(tmp_path):
    database = _workflow_database(tmp_path / "lifecycle-rollback.db")
    with closing(connect(database)) as connection:
        route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))
        before_events = connection.execute(
            "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0]
        with pytest.raises(ValueError, match="Invalid ticket transition"):
            transition_ticket_in_database(
                connection, "TKT-01", "closed", START + timedelta(minutes=2)
            )
        assert connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == "assigned"
        assert connection.execute(
            "SELECT unassigned_at FROM assignment_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = 'TKT-01'"
        ).fetchone()[0] == before_events


def test_reopen_preserves_lifecycle_and_sla_history(tmp_path):
    database = _workflow_database(tmp_path / "reopen-history.db")
    with closing(connect(database)) as connection:
        route_ticket_in_database(connection, "TKT-01", now=START + timedelta(minutes=1))
        transition_ticket_in_database(
            connection, "TKT-01", "in_progress", START + timedelta(minutes=2)
        )
        transition_ticket_in_database(
            connection, "TKT-01", "resolved", START + timedelta(minutes=10)
        )
        original_event_ids = {
            row["event_id"]
            for row in connection.execute(
                "SELECT event_id FROM ticket_events WHERE ticket_id = 'TKT-01'"
            )
        }
        original_sla_ids = {
            row["sla_id"]
            for row in connection.execute(
                "SELECT sla_id FROM sla_events WHERE ticket_id = 'TKT-01'"
            )
        }

        transition_ticket_in_database(
            connection, "TKT-01", "open", START + timedelta(minutes=20)
        )

        current_event_ids = {
            row["event_id"]
            for row in connection.execute(
                "SELECT event_id FROM ticket_events WHERE ticket_id = 'TKT-01'"
            )
        }
        current_sla_ids = {
            row["sla_id"]
            for row in connection.execute(
                "SELECT sla_id FROM sla_events WHERE ticket_id = 'TKT-01'"
            )
        }
        assert original_event_ids <= current_event_ids
        assert original_sla_ids < current_sla_ids
        assert connection.execute(
            "SELECT COUNT(DISTINCT started_at) FROM sla_events "
            "WHERE ticket_id = 'TKT-01' AND sla_type = 'resolution'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT event_type FROM ticket_events WHERE ticket_id = 'TKT-01' "
            "ORDER BY julianday(event_at) DESC, rowid DESC LIMIT 1"
        ).fetchone()[0] == "reopened"
