from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import hashlib
import json
import mimetypes
from pathlib import Path
import secrets
import subprocess
import time
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError


def output_media_type(record: dict[str, Any]) -> str:
    explicit = str(record.get("media_type") or "")
    if explicit:
        return explicit
    guessed, _ = mimetypes.guess_type(str(record.get("filename") or ""))
    return guessed or "application/octet-stream"


def enrich_output_record(record: dict[str, Any]) -> dict[str, Any]:
    return {**record, "media_type": output_media_type(record)}


def output_sets(raw: str) -> dict[str, list[dict[str, str]]]:
    value = json.loads(raw or "[]")
    if isinstance(value, list):
        return {"final": value, "stage1": []}
    return {"final": value.get("final", []), "stage1": value.get("stage1", [])}


def image_key(image: dict[str, str]) -> tuple[str, str, str]:
    return (
        image.get("filename", ""),
        image.get("subfolder", ""),
        image.get("type", ""),
    )


def remove_images(
    outputs: dict[str, list[dict[str, str]]],
    targets: dict[str, set[tuple[str, str, str]]],
) -> dict[str, list[dict[str, str]]]:
    return {
        kind: [
            image for image in images
            if image_key(image) not in targets.get(kind, set())
        ]
        for kind, images in outputs.items()
    }


def remove_output_files(
    images: Iterable[dict[str, str]],
    safe_output_path: Callable[[dict[str, str]], Path | None],
) -> None:
    for image in images:
        path = safe_output_path(image)
        if path and path.exists():
            path.unlink()


def prune_thumbnail_cache(
    thumbnail_dir: Path,
    cache_limit: int,
    cache_target: int,
) -> None:
    if not thumbnail_dir.exists():
        return
    files = [path for path in thumbnail_dir.glob("*.webp") if path.is_file()]
    stale_before = time.time() - 30 * 24 * 60 * 60
    for path in list(files):
        try:
            if path.stat().st_atime < stale_before:
                path.unlink()
                files.remove(path)
        except OSError:
            continue
    total = sum(path.stat().st_size for path in files)
    if total <= cache_limit:
        return
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        try:
            total -= path.stat().st_size
            path.unlink()
        except OSError:
            continue
        if total <= cache_target:
            break


def thumbnail_for(
    path: Path,
    size: int,
    *,
    thumbnail_dir: Path,
    allowed_sizes: set[int],
    cache_limit: int,
    cache_target: int,
) -> Path:
    if size not in allowed_sizes:
        raise HTTPException(400, "Unsupported preview size")
    stat = path.stat()
    cache_key = hashlib.sha256(
        f"v2:{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}:{size}".encode()
    ).hexdigest()
    target = thumbnail_dir / f"{cache_key}.webp"
    if target.is_file():
        target.touch()
        return target
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".{secrets.token_hex(4)}.tmp")
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((size, size * 4), Image.Resampling.LANCZOS)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        image.save(temporary, format="WEBP", quality=82, method=4)
    temporary.replace(target)
    prune_thumbnail_cache(thumbnail_dir, cache_limit, cache_target)
    return target


def video_thumbnail_for(
    path: Path,
    size: int,
    *,
    thumbnail_dir: Path,
    allowed_sizes: set[int],
    cache_limit: int,
    cache_target: int,
    ffmpeg_path: Path,
    media_process_flags: Callable[[], int],
) -> Path:
    """Extract and cache the first decoded video frame as a WebP poster."""
    if size not in allowed_sizes:
        raise HTTPException(400, "Unsupported preview size")
    if not ffmpeg_path.is_file():
        raise HTTPException(503, "Video preview component is unavailable")
    stat = path.stat()
    cache_key = hashlib.sha256(
        f"video-v1:{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}:{size}".encode()
    ).hexdigest()
    target = thumbnail_dir / f"{cache_key}.webp"
    if target.is_file():
        target.touch()
        return target
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(4)
    temporary_frame = thumbnail_dir / f"{cache_key}.{token}.png"
    temporary_webp = thumbnail_dir / f"{cache_key}.{token}.webp"
    try:
        result = subprocess.run(
            [
                str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
                "-vf", f"scale={size}:{size * 4}:force_original_aspect_ratio=decrease",
                str(temporary_frame),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=media_process_flags(),
            check=False,
        )
        if result.returncode or not temporary_frame.is_file():
            raise HTTPException(415, "Video first frame could not be decoded")
        with Image.open(temporary_frame) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.save(temporary_webp, format="WEBP", quality=82, method=4)
        temporary_webp.replace(target)
    except HTTPException:
        raise
    except (OSError, subprocess.SubprocessError, UnidentifiedImageError) as exc:
        raise HTTPException(415, "Video first frame could not be decoded") from exc
    finally:
        temporary_frame.unlink(missing_ok=True)
        temporary_webp.unlink(missing_ok=True)
    prune_thumbnail_cache(thumbnail_dir, cache_limit, cache_target)
    return target


def private_media_cache_headers(
    max_age: int | None = None,
    *,
    stale_while_revalidate: int | None = None,
    accept_ranges: bool = False,
) -> dict[str, str]:
    if max_age is None:
        cache_control = "private, no-cache, max-age=0, must-revalidate"
    else:
        cache_control = f"private, max-age={max_age}"
        if stale_while_revalidate:
            cache_control += f", stale-while-revalidate={stale_while_revalidate}"
    headers = {"Cache-Control": cache_control, "Vary": "Cookie"}
    if accept_ranges:
        headers["Accept-Ranges"] = "bytes"
    return headers


async def private_video_poster_response(
    path: Path, size: int, *, create_thumbnail: Callable[[Path, int], Path],
) -> Response:
    """Keep video navigation usable if the optional poster tool is unavailable."""
    try:
        target = await asyncio.to_thread(create_thumbnail, path, size)
    except HTTPException as exc:
        if exc.status_code not in {415, 503}:
            raise
        message = "请配置 FFmpeg 后查看封面" if exc.status_code == 503 else "暂时无法读取视频封面"
        return Response(
            '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">'
            '<rect width="640" height="360" fill="#202329"/>'
            '<path d="M300 95 L300 145 L345 120 Z" fill="#c8cbd0"/>'
            '<g fill="#c8cbd0" text-anchor="middle" font-family="sans-serif" font-size="20">'
            f'<text x="320" y="200">{message}</text>'
            '<text x="320" y="236" font-size="16">仍可打开并播放视频</text></g></svg>',
            media_type="image/svg+xml",
            headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
        )
    return FileResponse(target, media_type="image/webp", headers=private_media_cache_headers())


async def private_image_response(
    path: Path,
    preview: int | None,
    *,
    create_thumbnail: Callable[[Path, int], Path],
    media_max_age: int,
    preview_max_age: int,
    preview_stale_while_revalidate: int,
) -> FileResponse:
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if media_type.startswith("video/") or media_type.startswith("audio/"):
        return FileResponse(
            path,
            media_type=media_type,
            headers=private_media_cache_headers(media_max_age),
        )
    if preview is None:
        return FileResponse(
            path,
            media_type=media_type,
            headers=private_media_cache_headers(),
        )
    target = await asyncio.to_thread(create_thumbnail, path, preview)
    return FileResponse(
        target,
        media_type="image/webp",
        headers=private_media_cache_headers(
            preview_max_age,
            stale_while_revalidate=preview_stale_while_revalidate,
        ),
    )
