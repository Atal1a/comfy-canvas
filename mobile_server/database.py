from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


def connect_database(path: Path, *, rows: bool = False) -> sqlite3.Connection:
    """Open a consistently configured SQLite connection."""
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    if rows:
        connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database_session(
    path: Path,
    *,
    rows: bool = False,
    write: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Close every connection and commit or roll back explicit write scopes."""
    connection = connect_database(path, rows=rows)
    try:
        yield connection
        if write:
            connection.commit()
    except Exception:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()
