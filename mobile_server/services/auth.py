from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import unicodedata
from typing import Any

from fastapi import HTTPException


def normalize_username(value: Any) -> tuple[str, str]:
    """Return the display spelling and the canonical lookup key."""
    display = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not 2 <= len(display) <= 32 or any(ord(char) < 32 for char in display):
        raise HTTPException(400, "用户名长度必须为 2 到 32 个字符")
    return display, display.casefold()


def password_hash(password: str) -> str:
    """Hash an account password using the existing on-disk format."""
    if not 10 <= len(password) <= 128:
        raise HTTPException(400, "密码长度必须为 10 到 128 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=32768, r=8, p=1,
        maxmem=64 * 1024 * 1024,
    )
    return (
        "scrypt$32768$8$1$" + base64.urlsafe_b64encode(salt).decode()
        + "$" + base64.urlsafe_b64encode(digest).decode()
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify both current and previously stored scrypt password values."""
    try:
        _, n, r, p, salt, expected = encoded.split("$", 5)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.urlsafe_b64decode(salt),
            n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(digest, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    """Store only a one-way session token digest in SQLite."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
