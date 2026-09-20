from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DirectoryMetrics(BaseModel):
    bytes: int = 0
    files: int = 0


class DatabaseDiagnostics(BaseModel):
    quick_check: str
    files: dict[str, int] = Field(default_factory=dict)
    rows: dict[str, int] = Field(default_factory=dict)


class ComfyDiagnostics(BaseModel):
    model_config = ConfigDict(extra="allow")

    reachable: bool
    status: int | None = None
    checked_at: str | None = None
    error: str | None = None


class AdminDiagnosticsResponse(BaseModel):
    generated_at: str
    cached_at: str | None = None
    stale: bool = False
    database: DatabaseDiagnostics
    queue: dict[str, int] = Field(default_factory=dict)
    storage: dict[str, DirectoryMetrics] = Field(default_factory=dict)
    frontend: DirectoryMetrics = Field(default_factory=DirectoryMetrics)
    comfyui: ComfyDiagnostics
    errors: dict[str, str] | None = None


class AccountStorageResponse(BaseModel):
    used_bytes: int
    quota_bytes: int | None
