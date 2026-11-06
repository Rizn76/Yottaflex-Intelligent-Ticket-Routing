"""SQLite connection and schema initialization helpers."""
from __future__ import annotations

import sqlite3
import argparse
from contextlib import closing, contextmanager
from itertools import count
from pathlib import Path
from typing import Generator

from config import load_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"
MIGRATION_PATH = PROJECT_ROOT / "sql" / "migrations" / "002_history_integrity.sql"
SCHEMA_VERSION = 2
_SAVEPOINTS = count()


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite database with foreign-key enforcement enabled."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Generator[None, None, None]:
    """Serialize a workflow transaction, supporting callers already in a transaction."""
    nested = connection.in_transaction
    savepoint = f"yottaflex_workflow_{next(_SAVEPOINTS)}"
    connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
        else:
            connection.rollback()
        raise
    else:
        connection.execute(f"RELEASE {savepoint}" if nested else "COMMIT")


def _validate_database_integrity(connection: sqlite3.Connection) -> None:
    checks = {
        "ticket timestamps are invalid or out of order": """
            SELECT COUNT(*) FROM tickets
            WHERE julianday(created_at) IS NULL
               OR julianday(updated_at) IS NULL
               OR julianday(updated_at) < julianday(created_at)
        """,
        "channel hours or timezone differ from their business calendar": """
            SELECT COUNT(*)
            FROM channels
            JOIN business_calendars USING (calendar_id)
            WHERE channels.timezone <> business_calendars.timezone
               OR channels.business_hours_start <> business_calendars.business_hours_start
               OR channels.business_hours_end <> business_calendars.business_hours_end
        """,
        "message history predates or conflicts with its ticket": """
            SELECT COUNT(*)
            FROM messages
            JOIN tickets USING (ticket_id)
            WHERE julianday(messages.created_at) IS NULL
               OR julianday(messages.created_at) < julianday(tickets.created_at)
               OR messages.channel_id <> tickets.channel_id
        """,
        "SLA events use a calendar different from their ticket channel": """
            SELECT COUNT(*)
            FROM sla_events
            JOIN tickets USING (ticket_id)
            JOIN channels ON channels.channel_id = tickets.channel_id
            WHERE sla_events.calendar_id <> channels.calendar_id
        """,
    }
    for description, query in checks.items():
        count = int(connection.execute(query).fetchone()[0])
        if count:
            raise sqlite3.IntegrityError(
                f"Database integrity check failed: {description} ({count} row(s))."
            )

    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            f"Database has {len(violations)} foreign-key violation(s): {violations[:5]}"
        )


def _install_primary_key_guards(connection: sqlite3.Connection) -> None:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for table_row in tables:
        table_name = str(table_row["name"])
        quoted_table = '"' + table_name.replace('"', '""') + '"'
        columns = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
        primary_keys = sorted(
            (column for column in columns if column["pk"]),
            key=lambda column: column["pk"],
        )
        if not primary_keys:
            raise sqlite3.IntegrityError(f"Table {table_name} is missing a primary key.")

        for column in primary_keys:
            if column["notnull"] or (
                str(column["type"]).upper() == "INTEGER" and column["pk"] == 1
            ):
                continue

            column_name = str(column["name"])
            quoted_column = '"' + column_name.replace('"', '""') + '"'
            empty_key_count = connection.execute(
                f"""
                SELECT COUNT(*) FROM {quoted_table}
                WHERE {quoted_column} IS NULL
                   OR length(trim(CAST({quoted_column} AS TEXT))) = 0
                """
            ).fetchone()[0]
            if empty_key_count:
                raise sqlite3.IntegrityError(
                    f"Table {table_name}.{column_name} has "
                    f"{empty_key_count} null or empty primary-key value(s)."
                )

            for operation, update_columns in (
                ("insert", ""),
                ("update", f" OF {quoted_column}"),
            ):
                trigger_name = f"enforce_pk_{table_name}_{column_name}_{operation}"
                quoted_trigger = '"' + trigger_name.replace('"', '""') + '"'
                connection.executescript(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {quoted_trigger}
                    BEFORE {operation.upper()}{update_columns} ON {quoted_table}
                    WHEN NEW.{quoted_column} IS NULL
                      OR length(trim(CAST(NEW.{quoted_column} AS TEXT))) = 0
                    BEGIN
                        SELECT RAISE(ABORT, 'primary key values must not be null or empty');
                    END;
                    """
                )


def initialize_database(database_path: str | Path, schema_path: str | Path = SCHEMA_PATH) -> None:
    """Create or safely upgrade the schema, preserving existing records."""
    schema_file = Path(schema_path)
    schema = schema_file.read_text(encoding="utf-8")
    uses_project_schema = schema_file.resolve() == SCHEMA_PATH.resolve()
    migration = MIGRATION_PATH.read_text(encoding="utf-8") if uses_project_schema else None
    with closing(connect(database_path)) as connection:
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if uses_project_schema and current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than supported "
                f"version {SCHEMA_VERSION}."
            )

        connection.executescript(schema)
        if uses_project_schema and current_version < SCHEMA_VERSION:
            assert migration is not None
            connection.executescript(migration)
        connection.executescript(schema)
        if uses_project_schema:
            _install_primary_key_guards(connection)
            _validate_database_integrity(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def foreign_key_violations(database_path: str | Path) -> list[sqlite3.Row]:
    """Return all SQLite foreign-key violations for an initialized database."""
    with closing(connect(database_path)) as connection:
        return connection.execute("PRAGMA foreign_key_check").fetchall()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize the YottaFlex SQLite schema.")
    config = load_config()
    parser.add_argument("--database", default=str(Path(config.output_dir) / "tickets.sqlite"))
    parser.add_argument("--schema", default=str(SCHEMA_PATH))
    args = parser.parse_args()
    initialize_database(args.database, args.schema)
    print(f"Initialized SQLite schema at {Path(args.database).resolve()}")
