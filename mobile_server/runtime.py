from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles


COMPRESSIBLE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".map",
    ".svg",
    ".txt",
    ".webmanifest",
    ".xml",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in ("request_id", "method", "path", "status", "duration_ms", "user_id"):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_runtime_logging(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger("comfy_canvas")
    logger.setLevel(logging.INFO)
    if any(getattr(handler, "comfy_canvas_handler", False) for handler in logger.handlers):
        return logger
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "server.jsonl",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    handler.comfy_canvas_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.propagate = True
    return logger


def accepted_precompressed_path(request: Request, source: Path) -> tuple[Path, str | None]:
    if source.suffix.lower() not in COMPRESSIBLE_SUFFIXES or request.headers.get("range"):
        return source, None
    accepted = {
        value.split(";", 1)[0].strip().lower()
        for value in request.headers.get("accept-encoding", "").split(",")
    }
    for encoding, suffix in (("br", ".br"), ("gzip", ".gz")):
        candidate = Path(f"{source}{suffix}")
        if encoding in accepted and candidate.is_file():
            return candidate, encoding
    return source, None


def apply_content_encoding(response: Response, encoding: str | None, original_path: Path) -> None:
    if not encoding:
        return
    response.headers["Content-Encoding"] = encoding
    response.headers["Vary"] = "Accept-Encoding"
    media_type = mimetypes.guess_type(original_path.name)[0]
    if media_type:
        response.headers["Content-Type"] = media_type


def precompressed_file_response(request: Request, source: Path, media_type: str | None = None) -> FileResponse:
    selected, encoding = accepted_precompressed_path(request, source)
    response = FileResponse(selected, media_type=media_type or mimetypes.guess_type(source.name)[0])
    apply_content_encoding(response, encoding, source)
    return response


class PrecompressedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]) -> Response:
        request = Request(scope)
        original = Path(self.directory or "") / path
        selected, encoding = accepted_precompressed_path(request, original)
        selected_path = f"{path}{'.br' if encoding == 'br' else '.gz' if encoding == 'gzip' else ''}"
        response = await super().get_response(selected_path, scope)
        apply_content_encoding(response, encoding, original)
        return response


def apply_security_headers(response: Response) -> None:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self' ws: wss:"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
