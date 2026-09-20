from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException


def display_job_title(
    row: dict[str, Any],
    workflow_label: Callable[[str], str],
) -> str:
    value = str(row.get("title") or "").strip()
    if value:
        return value
    workflow_key = str(row.get("workflow_key") or "")
    return workflow_label(workflow_key)


def normalize_job_title(value: Any) -> str:
    title = unicodedata.normalize("NFKC", str(value or "")).strip()
    if len(title) > 80 or any(
        unicodedata.category(char) == "Cc" for char in title
    ):
        raise HTTPException(
            400,
            "Title must be a single line of no more than 80 characters",
        )
    return title
