from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
import json
import math
import os
from pathlib import Path
import secrets

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError


def normalized_avatar_crop(
    raw: str | None,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    if raw:
        try:
            value = json.loads(raw)
            x = float(value["x"])
            y = float(value["y"])
            crop_width = float(value["width"])
            crop_height = float(value["height"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, "头像裁切范围无效") from exc
        numbers = (x, y, crop_width, crop_height)
        if not all(math.isfinite(number) for number in numbers):
            raise HTTPException(400, "头像裁切范围无效")
        outside = (
            x < 0
            or y < 0
            or crop_width <= 0
            or crop_height <= 0
            or x + crop_width > 1.000001
            or y + crop_height > 1.000001
        )
        if outside:
            raise HTTPException(400, "头像裁切范围无效")
        left = max(0, min(width - 1, int(round(x * width))))
        top = max(0, min(height - 1, int(round(y * height))))
        right = max(
            left + 1,
            min(width, int(round((x + crop_width) * width))),
        )
        bottom = max(
            top + 1,
            min(height, int(round((y + crop_height) * height))),
        )
    else:
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        right = left + side
        bottom = top + side

    side = min(right - left, bottom - top)
    left += ((right - left) - side) // 2
    top += ((bottom - top) - side) // 2
    if side < 8:
        raise HTTPException(400, "头像裁切区域太小")
    return left, top, left + side, top + side


def write_avatar_file(
    user_id: int,
    contents: bytes,
    crop: str | None,
    *,
    avatar_dir: Path,
    avatar_path: Callable[[int], Path],
    max_pixels: int,
    avatar_size: int,
) -> None:
    try:
        with Image.open(BytesIO(contents)) as probe:
            detected = str(probe.format or "").upper()
            width, height = probe.size
            probe.verify()
        if detected not in {"JPEG", "PNG", "WEBP"}:
            raise HTTPException(415, "头像仅支持 JPEG、PNG 或 WebP")
        if width < 8 or height < 8 or width * height > max_pixels:
            raise HTTPException(413, "头像尺寸过大或过小")
        with Image.open(BytesIO(contents)) as source:
            source = ImageOps.exif_transpose(source)
            box = normalized_avatar_crop(crop, source.width, source.height)
            avatar = source.crop(box).convert("RGB").resize(
                (avatar_size, avatar_size),
                Image.Resampling.LANCZOS,
            )
            avatar_dir.mkdir(parents=True, exist_ok=True)
            target = avatar_path(user_id)
            temporary = avatar_dir / (
                f".{int(user_id)}-{secrets.token_hex(6)}.webp"
            )
            try:
                avatar.save(temporary, "WEBP", quality=88, method=6)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    except HTTPException:
        raise
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise HTTPException(400, "头像图片无法读取") from exc
