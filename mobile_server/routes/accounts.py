from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse


@dataclass(frozen=True)
class AccountRouteDependencies:
    current_user: Callable[[], dict[str, Any]]
    normalize_username: Callable[[Any], tuple[str, str]]
    utc_now: Callable[[], str]
    database_path: Callable[[], Path]
    db_user: Callable[[int], dict[str, Any] | None]
    public_user: Callable[[dict[str, Any]], dict[str, Any]]
    broadcast_profile_update: Callable[[dict[str, Any]], Awaitable[None]]
    avatar_path: Callable[[int], Path]
    write_avatar_file: Callable[[int, bytes, str | None], None]
    db_execute: Callable[[str, tuple[Any, ...]], None]
    verify_password: Callable[[str, str], bool]
    password_hash: Callable[[str], str]
    token_digest: Callable[[str], str]
    allowed_avatar_types: set[str]
    max_avatar_bytes: int
    session_cookie: str


def create_account_router(deps: AccountRouteDependencies) -> APIRouter:
    router = APIRouter()

    @router.patch("/api/account/profile")
    async def update_account_profile(
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = deps.current_user()
        display, username_key = deps.normalize_username(payload.get("username"))
        now = deps.utc_now()
        try:
            with sqlite3.connect(deps.database_path()) as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT user_id FROM users "
                    "WHERE username_key=? AND user_id<>?",
                    (username_key, user["user_id"]),
                ).fetchone()
                if existing:
                    raise HTTPException(409, "用户名已存在")
                db.execute(
                    "UPDATE users SET username=?,username_key=?,updated_at=? "
                    "WHERE user_id=?",
                    (display, username_key, now, user["user_id"]),
                )
                db.execute(
                    "UPDATE chat_messages SET sender_name=? WHERE sender_id=?",
                    (display, user["user_id"]),
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "用户名已存在") from exc
        updated = deps.db_user(int(user["user_id"])) or user
        await deps.broadcast_profile_update(updated)
        return {"user": deps.public_user(updated)}

    @router.put("/api/account/avatar")
    async def update_account_avatar(
        image: Annotated[UploadFile, File()],
        crop: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        user = deps.current_user()
        if image.content_type not in deps.allowed_avatar_types:
            raise HTTPException(415, "头像仅支持 JPEG、PNG 或 WebP")
        contents = await image.read(deps.max_avatar_bytes + 1)
        if len(contents) > deps.max_avatar_bytes:
            raise HTTPException(413, "头像文件不能超过 10 MB")
        if not contents:
            raise HTTPException(400, "头像文件为空")
        await asyncio.to_thread(
            deps.write_avatar_file,
            int(user["user_id"]),
            contents,
            crop,
        )
        with sqlite3.connect(deps.database_path()) as db:
            db.execute(
                "UPDATE users SET avatar_version=avatar_version+1,updated_at=? "
                "WHERE user_id=?",
                (deps.utc_now(), user["user_id"]),
            )
        updated = deps.db_user(int(user["user_id"])) or user
        await deps.broadcast_profile_update(updated)
        return {"user": deps.public_user(updated)}

    @router.delete("/api/account/avatar")
    async def delete_account_avatar() -> dict[str, Any]:
        user = deps.current_user()
        path = deps.avatar_path(int(user["user_id"]))
        existed = path.is_file() or int(user.get("avatar_version") or 0) > 0
        path.unlink(missing_ok=True)
        if existed:
            deps.db_execute(
                "UPDATE users SET avatar_version=avatar_version+1,updated_at=? "
                "WHERE user_id=?",
                (deps.utc_now(), user["user_id"]),
            )
        updated = deps.db_user(int(user["user_id"])) or user
        await deps.broadcast_profile_update(updated)
        return {"user": deps.public_user(updated)}

    @router.post("/api/account/password")
    async def update_account_password(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = deps.current_user()
        current_password = str(payload.get("current_password") or "")
        new_password = str(payload.get("new_password") or "")
        if not deps.verify_password(current_password, user["password_hash"]):
            raise HTTPException(401, "当前密码不正确")
        if deps.verify_password(new_password, user["password_hash"]):
            raise HTTPException(400, "新密码不能与当前密码相同")
        new_hash = deps.password_hash(new_password)
        token = request.cookies.get(deps.session_cookie)
        current_hash = deps.token_digest(token) if token else ""
        with sqlite3.connect(deps.database_path()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE users SET password_hash=?,updated_at=? WHERE user_id=?",
                (new_hash, deps.utc_now(), user["user_id"]),
            )
            cursor = db.execute(
                "DELETE FROM sessions WHERE user_id=? AND session_hash<>?",
                (user["user_id"], current_hash),
            )
            revoked = max(0, int(cursor.rowcount or 0))
        return {"changed": True, "other_sessions_revoked": revoked}

    @router.get("/api/users/{user_id}/avatar")
    async def user_avatar(
        user_id: int,
        v: int | None = None,
    ) -> FileResponse:
        user = deps.db_user(user_id)
        version = int(user.get("avatar_version") or 0) if user else 0
        path = deps.avatar_path(user_id)
        invalid = (
            not user
            or user["disabled"]
            or version <= 0
            or (v is not None and v != version)
            or not path.is_file()
        )
        if invalid:
            raise HTTPException(404, "头像不存在")
        return FileResponse(
            path,
            media_type="image/webp",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "ETag": f'"avatar-{user_id}-{version}"',
            },
        )

    router.update_account_profile = update_account_profile  # type: ignore[attr-defined]
    router.update_account_avatar = update_account_avatar  # type: ignore[attr-defined]
    router.delete_account_avatar = delete_account_avatar  # type: ignore[attr-defined]
    router.update_account_password = update_account_password  # type: ignore[attr-defined]
    router.user_avatar = user_avatar  # type: ignore[attr-defined]
    return router
