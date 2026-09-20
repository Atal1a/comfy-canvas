from __future__ import annotations

import sqlite3


CURRENT_SCHEMA_VERSION = 1


def ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )


def record_schema_baseline(connection: sqlite3.Connection, applied_at: str) -> None:
    """Record the current portable schema after its idempotent bootstrap succeeds."""
    ensure_migration_table(connection)
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
        (CURRENT_SCHEMA_VERSION, "portable-schema-baseline", applied_at),
    )
