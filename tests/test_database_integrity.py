import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from config import Config, load_config
from src.database import connect, foreign_key_violations, initialize_database
from src.generator import generate
from src.sla_engine import add_business_minutes


EXPECTED_TABLES = {
    "business_calendars",
    "calendar_business_days",
    "calendar_holidays",
    "customers",
    "channels",
    "languages",
    "intents",
    "priority_rules",
    "agents",
    "skills",
    "agent_skills",
    "agent_languages",
    "queues",
    "tickets",
    "messages",
    "assignment_events",
    "sla_events",
    "ticket_events",
    "resolutions",
    "anomalies",
    "ground_truth_labels",
}


@pytest.fixture(scope="module")
def populated_database(tmp_path_factory):
    output = tmp_path_factory.mktemp("schema-integrity") / "data"
    generate(
        seed=23,
        tickets=10,
        agents=5,
        messages=15,
        event_days=2,
        output_dir=output,
    )
    return output / "tickets.sqlite"


def test_schema_has_all_tables_and_explicit_primary_keys(populated_database):
    with closing(connect(populated_database)) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert EXPECTED_TABLES <= tables

        for table in EXPECTED_TABLES:
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            primary_key_columns = [column for column in columns if column["pk"]]
            assert primary_key_columns, f"{table} has no primary key"
            assert all(column["notnull"] for column in primary_key_columns), table

        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert foreign_key_violations(populated_database) == []


def test_primary_keys_and_foreign_keys_reject_invalid_records(populated_database):
    with pytest.raises(sqlite3.IntegrityError):
        with closing(connect(populated_database)) as connection, connection:
            connection.execute(
                "INSERT INTO languages (language_id, language_name) VALUES (?, ?)",
                (None, "New language"),
            )

    with closing(connect(populated_database)) as connection:
        ticket = connection.execute(
            "SELECT ticket_id FROM tickets LIMIT 1"
        ).fetchone()["ticket_id"]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tickets SELECT * FROM tickets WHERE ticket_id = ?",
                (ticket,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO agent_skills (agent_id, skill_id) VALUES (?, ?)",
                ("AGT-MISSING", "SK-MISSING"),
            )


def test_ticket_event_enums_and_states_are_constrained(populated_database):
    with closing(connect(populated_database)) as connection:
        ticket_id = connection.execute(
            "SELECT ticket_id FROM tickets LIMIT 1"
        ).fetchone()["ticket_id"]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO ticket_events
                    (event_id, ticket_id, event_type, event_at, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("EVT-BAD-TYPE", ticket_id, "not_a_real_event", "2026-01-01T00:00:00", "bad"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO ticket_events
                    (event_id, ticket_id, event_type, from_state, to_state, event_at, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("EVT-BAD-STATE", ticket_id, "opened", "not_a_state", "open",
                 "2026-01-01T00:00:00", "bad"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tickets SET priority = 'urgent' WHERE ticket_id = ?",
                (ticket_id,),
            )


def test_ticket_and_message_timestamps_are_valid_and_ordered(populated_database):
    with closing(connect(populated_database)) as connection:
        ticket = connection.execute(
            "SELECT ticket_id, created_at, channel_id FROM tickets LIMIT 1"
        ).fetchone()
        ticket_id = ticket["ticket_id"]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tickets SET updated_at = 'not-a-timestamp' WHERE ticket_id = ?",
                (ticket_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tickets SET updated_at = '2000-01-01T00:00:00' WHERE ticket_id = ?",
                (ticket_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, ticket_id, channel_id, sender_type, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("MSG-TOO-EARLY", ticket_id, ticket["channel_id"], "customer",
                 "historical message", "2000-01-01T00:00:00"),
            )

        other_channel = connection.execute(
            "SELECT channel_id FROM channels WHERE channel_id <> ? LIMIT 1",
            (ticket["channel_id"],),
        ).fetchone()["channel_id"]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, ticket_id, channel_id, sender_type, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("MSG-WRONG-CHANNEL", ticket_id, other_channel, "customer",
                 "wrong channel", ticket["created_at"]),
            )


def test_channel_and_sla_calendars_match_the_configured_channel(populated_database):
    with closing(connect(populated_database)) as connection:
        channel = connection.execute(
            "SELECT channel_id FROM channels LIMIT 1"
        ).fetchone()["channel_id"]
        with pytest.raises(sqlite3.IntegrityError, match="match its business calendar"):
            connection.execute(
            "UPDATE channels SET timezone = 'UTC' WHERE channel_id = ?",
            (channel,),
            )

        original = connection.execute(
            "SELECT * FROM sla_events WHERE sla_type = 'first_response' LIMIT 1"
        ).fetchone()
        other_calendar = connection.execute(
            "SELECT calendar_id FROM business_calendars WHERE calendar_id <> ? LIMIT 1",
            (original["calendar_id"],),
        ).fetchone()["calendar_id"]
        with pytest.raises(sqlite3.IntegrityError, match="match the ticket channel calendar"):
            connection.execute(
            """
            INSERT INTO sla_events (
                sla_id, ticket_id, sla_type, target_minutes, started_at, due_at,
                actual_at, breached, breach_minutes, escalation_at, calendar_id
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, 0, NULL, ?)
            """,
            (
                "SLA-WRONG-CALENDAR",
                original["ticket_id"],
                original["sla_type"],
                original["target_minutes"],
                original["started_at"],
                original["due_at"],
                other_calendar,
            ),
            )


def test_sla_history_allows_repeated_types_and_is_immutable(populated_database):
    with closing(connect(populated_database)) as connection:
        original = connection.execute(
            "SELECT * FROM sla_events WHERE sla_type = 'first_response' LIMIT 1"
        ).fetchone()
        started = datetime.fromisoformat(original["started_at"]) + timedelta(days=1)
        due = started + timedelta(minutes=original["target_minutes"])
        connection.execute(
            """
            INSERT INTO sla_events (
                sla_id, ticket_id, sla_type, target_minutes, started_at, due_at,
                actual_at, breached, breach_minutes, escalation_at, calendar_id
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, 0, NULL, ?)
            """,
            (
                "SLA-HISTORY-SECOND",
                original["ticket_id"],
                original["sla_type"],
                original["target_minutes"],
                started.isoformat(),
                due.isoformat(),
                original["calendar_id"],
            ),
        )
        connection.commit()
        history = connection.execute(
            """
            SELECT sla_id, started_at FROM sla_events
            WHERE ticket_id = ? AND sla_type = 'first_response'
            ORDER BY started_at, sla_id
            """,
            (original["ticket_id"],),
        ).fetchall()
        assert len(history) == 2
        assert [row["started_at"] for row in history] == sorted(
            row["started_at"] for row in history
        )

    with closing(connect(populated_database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE sla_events SET breached = 1 WHERE sla_id = ?",
                ("SLA-HISTORY-SECOND",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM sla_events WHERE sla_id = ?",
                ("SLA-HISTORY-SECOND",),
            )


def test_ticket_has_at_most_one_anomaly(populated_database):
    with closing(connect(populated_database)) as connection:
        anomaly = connection.execute(
            "SELECT ticket_id FROM anomalies LIMIT 1"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO anomalies
                    (anomaly_id, ticket_id, anomaly_type, description,
                     expected_validation, ground_truth_label)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("ANM-DUPLICATE", anomaly["ticket_id"], "transfer_loop",
                 "duplicate fixture", "TRANSFER_LOOP", "expected_detection"),
            )


def test_ticket_lifecycle_events_are_append_only(populated_database):
    with closing(connect(populated_database)) as connection:
        event = connection.execute(
            "SELECT event_id FROM ticket_events LIMIT 1"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE ticket_events SET details = 'rewritten' WHERE event_id = ?",
                (event["event_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM ticket_events WHERE event_id = ?",
                (event["event_id"],),
            )


def test_schema_initialization_is_repeatable_and_preserves_rows(populated_database):
    with closing(connect(populated_database)) as connection:
        before = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("tickets", "messages", "sla_events", "ticket_events", "anomalies")
        }
    initialize_database(populated_database)
    with closing(connect(populated_database)) as connection:
        after = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in before
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert before == after


def test_environment_configuration_and_timezone_support(monkeypatch, tmp_path):
    config = load_config({
        "YOTTAFLEX_SEED": "31",
        "YOTTAFLEX_TICKETS": "15",
        "YOTTAFLEX_DATA_DIR": str(tmp_path / "configured"),
        "YOTTAFLEX_TIMEZONE": "UTC",
    })
    assert config == Config(
        seed=31,
        tickets=15,
        agents=100,
        messages=100_000,
        event_days=90,
        start_date="2026-08-01",
        output_dir=str(tmp_path / "configured"),
        timezone_name="UTC",
    )
    assert ZoneInfo("Asia/Kolkata").key == "Asia/Kolkata"

    monkeypatch.setenv("YOTTAFLEX_SEED", "37")
    monkeypatch.setenv("YOTTAFLEX_TIMEZONE", "UTC")
    monkeypatch.setenv("YOTTAFLEX_OUTPUT_DIR", str(tmp_path / "from-env"))
    assert add_business_minutes(datetime(2026, 1, 1, 10), 30).tzinfo == ZoneInfo("UTC")
    manifest = generate(tickets=5, agents=3, messages=0, event_days=1)
    assert manifest["seed"] == 37
    assert manifest["configuration"]["timezone_name"] == "UTC"
    assert (tmp_path / "from-env" / "tickets.sqlite").exists()


def test_invalid_timezone_configuration_is_reported():
    with pytest.raises(ZoneInfoNotFoundError):
        load_config({"YOTTAFLEX_TIMEZONE": "not/a-timezone"})


def test_version_one_history_migration_preserves_rows(populated_database, tmp_path):
    legacy_database = tmp_path / "version-one.sqlite"
    shutil.copy2(populated_database, legacy_database)
    with closing(connect(legacy_database)) as connection:
        connection.execute("DROP TRIGGER protect_sla_event_update")
        connection.execute("DROP TRIGGER protect_sla_event_delete")
        connection.execute(
            "DELETE FROM sla_events WHERE sla_id = 'SLA-HISTORY-SECOND'"
        )
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("sla_events", "ticket_events", "resolutions")
        }
        connection.executescript(
            """
            CREATE TABLE sla_events__legacy (
                sla_id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL,
                sla_type TEXT NOT NULL CHECK (sla_type IN ('first_response', 'resolution')),
                target_minutes INTEGER NOT NULL CHECK (target_minutes > 0),
                started_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                actual_at TEXT,
                breached INTEGER NOT NULL CHECK (breached IN (0, 1)),
                breach_minutes INTEGER NOT NULL DEFAULT 0 CHECK (breach_minutes >= 0),
                escalation_at TEXT,
                calendar_id TEXT,
                FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE,
                FOREIGN KEY (calendar_id) REFERENCES business_calendars(calendar_id),
                UNIQUE (ticket_id, sla_type)
            );
            INSERT INTO sla_events__legacy SELECT * FROM sla_events;
            DROP TABLE sla_events;
            ALTER TABLE sla_events__legacy RENAME TO sla_events;

            CREATE TABLE ticket_events__legacy (
                event_id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                actor_id TEXT,
                event_at TEXT NOT NULL,
                details TEXT NOT NULL,
                anomaly_type TEXT,
                FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE,
                FOREIGN KEY (actor_id) REFERENCES agents(agent_id)
            );
            INSERT INTO ticket_events__legacy SELECT * FROM ticket_events;
            DROP TABLE ticket_events;
            ALTER TABLE ticket_events__legacy RENAME TO ticket_events;

            CREATE TABLE resolutions__legacy (
                resolution_id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL,
                resolved_at TEXT,
                resolution_code TEXT NOT NULL,
                resolved INTEGER NOT NULL CHECK (resolved IN (0, 1)),
                resolution_minutes INTEGER CHECK (resolution_minutes IS NULL OR resolution_minutes >= 0),
                csat_score INTEGER CHECK (csat_score IS NULL OR csat_score BETWEEN 1 AND 5),
                FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE
            );
            INSERT INTO resolutions__legacy SELECT * FROM resolutions;
            DROP TABLE resolutions;
            ALTER TABLE resolutions__legacy RENAME TO resolutions;
            PRAGMA user_version = 1;
            """
        )

    initialize_database(legacy_database)
    with closing(connect(legacy_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        ticket_id = connection.execute(
            "SELECT ticket_id FROM tickets LIMIT 1"
        ).fetchone()["ticket_id"]
        with pytest.raises(sqlite3.IntegrityError, match="primary key"):
            connection.execute(
                """
                INSERT INTO resolutions
                    (resolution_id, ticket_id, resolved_at, resolution_code,
                     resolved, resolution_minutes, csat_score)
                VALUES (?, ?, NULL, 'unresolved', 0, NULL, NULL)
                """,
                (None, ticket_id),
            )
        assert {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in counts
        } == counts
        sla_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sla_events'"
        ).fetchone()["sql"]
        assert "UNIQUE (ticket_id, sla_type)" not in sla_schema
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
