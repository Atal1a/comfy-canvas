from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException


GalleryRows = tuple[list[tuple[str, dict[str, Any]]], dict[str, set[str]]]


@dataclass(frozen=True)
class GalleryRouteDependencies:
    workflow_exists: Callable[[str], bool]
    gallery_record_rows: Callable[..., GalleryRows]
    load_gallery_hydration: Callable[..., Any]
    public_group: Callable[..., dict[str, Any]]
    public_job: Callable[..., dict[str, Any]]
    recycle_record_id: Callable[[dict[str, Any]], str]


def create_gallery_router(deps: GalleryRouteDependencies) -> APIRouter:
    router = APIRouter()

    def validate_filters(workflow_key: str | None, sort: str) -> None:
        if sort not in {"newest", "oldest", "title_asc", "title_desc"}:
            raise HTTPException(400, "Unsupported gallery sort")
        if workflow_key is not None and not deps.workflow_exists(workflow_key):
            raise HTTPException(404, "Workflow not found")

    def serialize_rows(
        rows: list[tuple[str, dict[str, Any]]],
        hidden_items: dict[str, set[str]],
    ) -> list[dict[str, Any]]:
        hydration = deps.load_gallery_hydration(rows, hidden_items)
        return [
            deps.public_group(row, hydration=hydration)
            if kind == "group"
            else deps.public_job(row, hydration=hydration)
            for kind, row in rows
        ]

    @router.get("/api/gallery")
    async def gallery(
        favorites: bool = False,
        collection_id: int | None = None,
        workflow_key: str | None = None,
        include_active: bool = False,
        limit: int | None = None,
        offset: int = 0,
        sort: str = "newest",
    ) -> list[dict[str, Any]]:
        if offset < 0:
            raise HTTPException(400, "Offset must be zero or greater")
        if limit is not None and not 1 <= limit <= 100:
            raise HTTPException(400, "Limit must be between 1 and 100")
        validate_filters(workflow_key, sort)
        rows, hidden_items = deps.gallery_record_rows(
            favorites=favorites,
            collection_id=collection_id,
            workflow_key=workflow_key,
            include_active=include_active,
            sort=sort,
        )
        page = rows[offset:offset + limit] if limit is not None else rows[offset:]
        return serialize_rows(page, hidden_items)

    @router.get("/api/gallery/context")
    async def gallery_context(
        record_id: str,
        radius: int = 24,
        favorites: bool = False,
        collection_id: int | None = None,
        workflow_key: str | None = None,
        sort: str = "newest",
    ) -> dict[str, Any]:
        if not 1 <= radius <= 50:
            raise HTTPException(400, "Radius must be between 1 and 50")
        validate_filters(workflow_key, sort)
        rows, hidden_items = deps.gallery_record_rows(
            favorites=favorites,
            collection_id=collection_id,
            workflow_key=workflow_key,
            sort=sort,
        )
        anchor = next((
            index for index, (_, row) in enumerate(rows)
            if deps.recycle_record_id(row) == record_id
        ), -1)
        if anchor < 0:
            raise HTTPException(404, "Gallery record not found")
        start = max(0, anchor - radius)
        end = min(len(rows), anchor + radius + 1)
        window = rows[start:end]
        return {
            "jobs": serialize_rows(window, hidden_items),
            "anchor_index": anchor - start,
            "has_before": start > 0,
            "has_after": end < len(rows),
        }

    router.gallery = gallery  # type: ignore[attr-defined]
    router.gallery_context = gallery_context  # type: ignore[attr-defined]
    return router
