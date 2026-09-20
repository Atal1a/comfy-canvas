from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any

from fastapi import HTTPException
from pypinyin import lazy_pinyin


GalleryRows = tuple[list[tuple[str, dict[str, Any]]], dict[str, set[str]]]


class GalleryRepository:
    """Owner-scoped gallery queries and ordering over the local SQLite store."""

    def __init__(
        self,
        database_path: Callable[[], Path],
        current_owner_id: Callable[[], int | None],
        recycle_record_id: Callable[[dict[str, Any]], str],
        stored_items: Callable[..., list[dict[str, Any]]],
        display_job_title: Callable[[dict[str, Any]], str],
    ) -> None:
        self._database_path = database_path
        self._current_owner_id = current_owner_id
        self._recycle_record_id = recycle_record_id
        self._stored_items = stored_items
        self._display_job_title = display_job_title

    @staticmethod
    def _has_materialized_items(row: dict[str, Any]) -> bool:
        try:
            items = json.loads(row.get("items_json") or "[]")
        except json.JSONDecodeError:
            return False
        return isinstance(items, list) and bool(items)

    def list_rows(
        self,
        favorites: bool = False,
        collection_id: int | None = None,
        workflow_key: str | None = None,
        include_active: bool = False,
        sort: str = "newest",
    ) -> GalleryRows:
        owner = self._current_owner_id()
        with closing(sqlite3.connect(self._database_path())) as db:
            db.row_factory = sqlite3.Row
            group_status = "status IN ('completed','cancelled')"
            if include_active:
                group_status = (
                    f"({group_status} OR (status IN "
                    "('queued','running','cancelling') "
                    "AND workflow_key!='minimax-h3'))"
                )
            group_query = (
                "SELECT * FROM generation_groups "
                f"WHERE {group_status} AND storage_scope='mobile'"
            )
            job_query = (
                "SELECT * FROM jobs WHERE status IN ('completed','cancelled') "
                "AND storage_scope='mobile' AND group_id IS NULL"
            )
            group_values: list[Any] = []
            job_values: list[Any] = []
            if owner:
                group_query += " AND owner_id=?"
                job_query += " AND owner_id=?"
                group_values.append(owner)
                job_values.append(owner)
            if workflow_key is not None:
                group_query += " AND workflow_key=?"
                job_query += " AND workflow_key=?"
                group_values.append(workflow_key)
                job_values.append(workflow_key)
            if collection_id is not None:
                collection = db.execute(
                    "SELECT 1 FROM favorite_collections "
                    "WHERE collection_id=? AND owner_id=?",
                    (collection_id, owner),
                ).fetchone()
                if not collection:
                    raise HTTPException(404, "Collection not found")
                group_query += (
                    " AND EXISTS (SELECT 1 FROM collection_memberships m "
                    "WHERE m.record_id=generation_groups.group_id "
                    "AND m.collection_id=?)"
                )
                job_query += (
                    " AND EXISTS (SELECT 1 FROM collection_memberships m "
                    "WHERE m.record_id=jobs.prompt_id AND m.collection_id=?)"
                )
                group_values.append(collection_id)
                job_values.append(collection_id)
            elif favorites:
                group_query += (
                    " AND EXISTS (SELECT 1 FROM collection_memberships m "
                    "WHERE m.record_id=generation_groups.group_id)"
                )
                job_query += (
                    " AND EXISTS (SELECT 1 FROM collection_memberships m "
                    "WHERE m.record_id=jobs.prompt_id)"
                )
            rows = [
                ("group", dict(row))
                for row in db.execute(group_query, tuple(group_values)).fetchall()
            ]
            rows += [
                ("job", dict(row))
                for row in db.execute(job_query, tuple(job_values)).fetchall()
            ]
            recycle_rows = db.execute(
                "SELECT kind,record_id,item_id FROM recycle_bin WHERE owner_id=?",
                (owner,),
            ).fetchall()
            fallback_group_ids = [
                str(row["group_id"])
                for kind, row in rows
                if kind == "group" and not self._has_materialized_items(row)
            ]
            fallback_children: dict[str, list[dict[str, Any]]] = {
                group_id: [] for group_id in fallback_group_ids
            }
            if fallback_group_ids:
                placeholders = ",".join("?" for _ in fallback_group_ids)
                for entry in db.execute(
                    f"SELECT * FROM jobs WHERE group_id IN ({placeholders}) "
                    "AND COALESCE(superseded,0)=0 "
                    "ORDER BY group_id,logical_index,submitted_at",
                    tuple(fallback_group_ids),
                ):
                    fallback_children.setdefault(
                        str(entry["group_id"]),
                        [],
                    ).append(dict(entry))

        trashed_tasks = {
            (str(kind), str(record_id))
            for kind, record_id, _ in recycle_rows
            if kind in {"group", "job"}
        }
        trashed_items: dict[str, set[str]] = {}
        for kind, record_id, item_id in recycle_rows:
            if kind == "item":
                trashed_items.setdefault(str(record_id), set()).add(str(item_id))

        visible_rows: list[tuple[str, dict[str, Any]]] = []
        for kind, row in rows:
            record_id = self._recycle_record_id(row)
            if (kind, record_id) in trashed_tasks:
                continue
            children = fallback_children.get(record_id) if kind == "group" else None
            items = self._stored_items(row, children)
            hidden = trashed_items.get(record_id, set())
            visible_items = [
                item for item in items
                if item.get("final") and str(item.get("id") or "") not in hidden
            ]
            if row.get("status") == "cancelled" and not visible_items:
                continue
            active_placeholder = (
                include_active
                and row.get("status") in {"queued", "running", "cancelling"}
            )
            if items and not visible_items and not active_placeholder:
                continue
            visible_rows.append((kind, row))

        if sort.startswith("title_"):
            def title_key(
                entry: tuple[str, dict[str, Any]],
            ) -> tuple[str, str]:
                title = self._display_job_title(entry[1])
                return "".join(lazy_pinyin(title)).casefold(), title.casefold()

            visible_rows.sort(key=self._time_key, reverse=True)
            visible_rows.sort(key=title_key, reverse=sort == "title_desc")
        else:
            visible_rows.sort(
                key=self._time_key,
                reverse=sort == "newest",
            )
        return visible_rows, trashed_items

    @staticmethod
    def _time_key(
        entry: tuple[str, dict[str, Any]],
    ) -> tuple[str, str]:
        row = entry[1]
        return (
            str(row.get("submitted_at") or ""),
            str(row.get("group_id") or row.get("prompt_id") or ""),
        )
