from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Annotated, Any

from fastapi import APIRouter, Body


@dataclass(frozen=True)
class WorkflowRouteDependencies:
    synchronize_lora_registry: Callable[..., Any]
    enabled_workflows: Callable[[], list[Any]]
    workflow_descriptor: Callable[[Any], dict[str, Any]]
    get_spec: Callable[[str], Any]
    normalize_settings: Callable[[Any, str], dict[str, Any]]
    source_dimensions: Callable[[int, int, float], tuple[int, int]]
    apply_history_estimate: Callable[[dict[str, Any]], dict[str, Any]]


def create_workflow_router(deps: WorkflowRouteDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/api/workflows")
    async def workflows() -> list[dict[str, Any]]:
        deps.synchronize_lora_registry()
        return [deps.workflow_descriptor(spec) for spec in deps.enabled_workflows()]

    @router.post("/api/workflows/minimax-h3/estimate")
    async def minimax_h3_estimate(
        settings: Annotated[dict[str, Any], Body()],
    ) -> dict[str, Any]:
        spec = deps.get_spec("minimax-h3")
        values = deps.normalize_settings(spec, json.dumps(settings))
        if values.get("mode") == "i2v":
            try:
                source_width = int(settings.get("source_width") or 0)
                source_height = int(settings.get("source_height") or 0)
            except (TypeError, ValueError):
                source_width = source_height = 0
            if source_width and source_height:
                values["width"], values["height"] = deps.source_dimensions(
                    source_width,
                    source_height,
                    values["megapixels"],
                )
                deps.apply_history_estimate(values)
        return {
            "available": values["estimate_source"] == "completed_h3_history",
            "estimated_seconds": values["estimated_seconds"],
            "estimate_range": values["estimate_range"],
            "samples": values["estimate_samples"],
        }

    router.workflows = workflows  # type: ignore[attr-defined]
    router.minimax_h3_estimate = minimax_h3_estimate  # type: ignore[attr-defined]
    return router
