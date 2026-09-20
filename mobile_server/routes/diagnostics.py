from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..schemas import AdminDiagnosticsResponse


def create_diagnostics_router(
    require_admin: Callable[[], dict[str, Any]],
    local_snapshot: Callable[[bool], dict[str, Any]],
    comfy_snapshot: Callable[[bool], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/diagnostics", response_model=AdminDiagnosticsResponse)
    async def admin_diagnostics(refresh: bool = False) -> dict[str, Any]:
        require_admin()
        local_result, comfy_result = await asyncio.gather(
            asyncio.to_thread(local_snapshot, refresh),
            comfy_snapshot(refresh),
            return_exceptions=True,
        )
        if isinstance(local_result, Exception):
            snapshot: dict[str, Any] = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "cached_at": None,
                "stale": True,
                "database": {"quick_check": "unavailable", "files": {}, "rows": {}},
                "queue": {}, "storage": {}, "frontend": {},
                "errors": {"local": str(local_result)},
            }
        else:
            snapshot = local_result
            snapshot["cached_at"] = snapshot.get("generated_at")
        if isinstance(comfy_result, Exception):
            snapshot["comfyui"] = {"reachable": False, "error": str(comfy_result)}
            snapshot["stale"] = True
        else:
            snapshot["comfyui"] = comfy_result
        return snapshot

    return router
