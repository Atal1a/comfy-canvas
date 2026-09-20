from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sqlite3
from typing import Any

from ..database import database_session


class AccountRepository:
    """Own account and session persistence without depending on FastAPI."""

    def __init__(self, database_path: Callable[[], Path]) -> None:
        self._database_path = database_path

    def user(self, user_id: int) -> dict[str, Any] | None:
        with database_session(self._database_path(), rows=True) as db:
            row = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_by_name(self, username_key: str) -> dict[str, Any] | None:
        with database_session(self._database_path(), rows=True) as db:
            row = db.execute(
                "SELECT * FROM users WHERE username_key=?", (username_key,),
            ).fetchone()
        return dict(row) if row else None

    def insert_user(
        self,
        display: str,
        username_key: str,
        encoded_password: str,
        role: str,
        now: str,
        default_collection_name: str,
    ) -> int:
        with sqlite3.connect(self._database_path()) as db:
            cursor = db.execute(
                "INSERT INTO users(username,username_key,password_hash,role,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (display, username_key, encoded_password, role, now, now),
            )
            user_id = int(cursor.lastrowid)
            legacy_default = role == "admin" and db.execute(
                "SELECT 1 FROM favorite_collections WHERE name=? AND owner_id IS NULL",
                (default_collection_name,),
            ).fetchone()
            if not legacy_default:
                db.execute(
                    "INSERT INTO favorite_collections(name,created_at,owner_id) VALUES(?,?,?)",
                    (default_collection_name, now, user_id),
                )
        return user_id

    def claim_legacy_data(self, user_id: int) -> None:
        with sqlite3.connect(self._database_path()) as db:
            tables = (
                "jobs", "generation_groups", "upscale_jobs", "detail_jobs",
                "preprocess_jobs", "favorite_collections",
            )
            for table in tables:
                db.execute(f"UPDATE {table} SET owner_id=? WHERE owner_id IS NULL", (user_id,))
            db.execute("UPDATE prompt_templates SET owner_id=NULL, is_system=1")

    def has_administrator(self) -> bool:
        with sqlite3.connect(self._database_path()) as db:
            return bool(db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone())

    def insert_session(
        self,
        session_hash: str,
        user_id: int,
        created_at: str,
        expires_at: str,
    ) -> None:
        with database_session(self._database_path(), write=True) as db:
            db.execute(
                "INSERT INTO sessions(session_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (session_hash, user_id, created_at, expires_at),
            )

    def session_user(self, session_hash: str, now: str) -> dict[str, Any] | None:
        with database_session(self._database_path(), rows=True) as db:
            row = db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.user_id=s.user_id "
                "WHERE s.session_hash=? AND s.expires_at>?",
                (session_hash, now),
            ).fetchone()
        if not row or row["disabled"]:
            return None
        return dict(row)
