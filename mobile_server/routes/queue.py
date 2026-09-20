from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, HTTPException


@dataclass(frozen=True)
class QueueRouteDependencies:
    current_user: Callable[[], dict[str, Any]]
    require_admin: Callable[[], dict[str, Any]]
    queue_snapshot: Callable[[int, bool], dict[str, Any]]
    queue_row: Callable[[int], dict[str, Any] | None]
    cancel_queued_task: Callable[[dict[str, Any]], Awaitable[None]]
    queue_lock: Callable[[], Any]
    recovery_status: Callable[[], dict[str, Any]]
    set_recovery_status: Callable[[str, str], None]
    restart_comfyui: Callable[[], Awaitable[None]]
    current_user_context: ContextVar[dict[str, Any] | None]
    set_performance_mode: Callable[[bool, int], Awaitable[None]]
    database_path: Callable[[], Path]
    db_user: Callable[[int], dict[str, Any] | None]
    db_queue_record: Callable[[str, str], dict[str, Any] | None]
    cancel_postprocess: Callable[[str, str], Awaitable[dict[str, Any]]]
    cancel_group: Callable[[str], Awaitable[dict[str, Any]]]


def create_queue_router(deps: QueueRouteDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/api/queue")
    async def shared_queue() -> dict[str, Any]:
        user = deps.current_user()
        return deps.queue_snapshot(int(user["user_id"]), user["role"] == "admin")

    @router.post("/api/queue/{queue_id}/cancel")
    async def cancel_shared_queue_task(queue_id: int) -> dict[str, bool]:
        user = deps.current_user()
        row = deps.queue_row(queue_id)
        if not row or (
            user["role"] != "admin"
            and int(row["owner_id"]) != int(user["user_id"])
        ):
            raise HTTPException(404, "Task was not found")
        await deps.cancel_queued_task(row)
        return {"cancelled": True}

    @router.get("/api/admin/queue")
    async def admin_queue() -> dict[str, Any]:
        user = deps.require_admin()
        return deps.queue_snapshot(int(user["user_id"]), True)

    @router.post("/api/admin/comfy-recovery")
    async def admin_comfy_recovery() -> dict[str, Any]:
        user = deps.require_admin()
        token = deps.current_user_context.set(None)
        try:
            async with deps.queue_lock():
                if deps.recovery_status()["recovery_state"] != "blocked":
                    raise HTTPException(409, "ComfyUI recovery is not blocked")
                deps.set_recovery_status(
                    "recovering",
                    "管理员正在恢复 ComfyUI；WebUI 保持运行。",
                )
                try:
                    await deps.restart_comfyui()
                except Exception as exc:
                    message = f"ComfyUI manual recovery failed: {exc}"
                    deps.set_recovery_status("blocked", message)
                    raise HTTPException(503, message) from exc
                deps.set_recovery_status("idle", "")
                return deps.queue_snapshot(int(user["user_id"]), True)
        finally:
            deps.current_user_context.reset(token)

    @router.put("/api/admin/performance-mode")
    async def admin_performance_mode(
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = deps.require_admin()
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled must be true or false")
        await deps.set_performance_mode(enabled, int(user["user_id"]))
        return deps.queue_snapshot(int(user["user_id"]), True)

    @router.post("/api/admin/queue/{queue_id}/run-next")
    async def admin_queue_run_next(queue_id: int) -> dict[str, Any]:
        user = deps.require_admin()
        row = deps.queue_row(queue_id)
        if not row or row["state"] not in {"waiting", "paused"}:
            raise HTTPException(409, "Only waiting tasks can be moved")
        with sqlite3.connect(deps.database_path()) as db:
            priority = int(db.execute(
                "SELECT COALESCE(MAX(priority),0)+1 FROM task_queue",
            ).fetchone()[0])
            db.execute(
                "UPDATE task_queue SET priority=? WHERE queue_id=?",
                (priority, queue_id),
            )
        return deps.queue_snapshot(int(user["user_id"]), True)

    @router.get("/api/admin/activity")
    async def admin_activity() -> list[dict[str, Any]]:
        user = deps.require_admin()
        snapshot = deps.queue_snapshot(int(user["user_id"]), True)
        return [
            {
                "id": item["record_id"],
                "queue_id": item["id"],
                "status": "running" if item["state"] == "running" else "queued",
                "submitted_at": item["queued_at"],
                "username": item["owner"],
                "kind": item["kind"],
                "position": item["position"],
                "eta_low": item["eta_low"],
                "eta_high": item["eta_high"],
            }
            for item in snapshot["tasks"]
        ]

    @router.post("/api/admin/activity/{kind}/{record_id}/cancel")
    async def admin_cancel_activity(kind: str, record_id: str) -> dict[str, bool]:
        deps.require_admin()
        mapping = {
            "generation": ("generation_groups", "group_id", None),
            "preprocess": ("preprocess_jobs", "preprocess_id", "preprocess"),
            "pre-upscale": ("preprocess_jobs", "preprocess_id", "preprocess"),
            "flux-detail": ("detail_jobs", "detail_id", "detail"),
            "upscale": ("upscale_jobs", "upscale_id", "upscale"),
            "prompt_tool": ("prompt_tool_jobs", "tool_id", "prompt_tool"),
        }
        if kind not in mapping:
            raise HTTPException(404, "任务不存在")
        table, id_column, post_kind = mapping[kind]
        with sqlite3.connect(deps.database_path()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                f"SELECT owner_id FROM {table} WHERE {id_column}=?",
                (record_id,),
            ).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        owner = deps.db_user(int(row["owner_id"]))
        if not owner:
            raise HTTPException(404, "任务所有者不存在")
        token = deps.current_user_context.set(owner)
        try:
            if post_kind == "prompt_tool":
                queued = deps.db_queue_record("prompt_tool", record_id)
                if queued:
                    await deps.cancel_queued_task(queued)
            elif post_kind:
                await deps.cancel_postprocess(post_kind, record_id)
            else:
                await deps.cancel_group(record_id)
        finally:
            deps.current_user_context.reset(token)
        return {"cancelled": True}

    # Temporary compatibility exports for direct function-level callers while
    # route registration moves out of the legacy composition module.
    router.cancel_shared_queue_task = cancel_shared_queue_task  # type: ignore[attr-defined]
    router.admin_activity = admin_activity  # type: ignore[attr-defined]
    return router
