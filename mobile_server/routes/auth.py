from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class AuthRouteDependencies:
    normalize_username: Callable[[Any], tuple[str, str]]
    password_hash: Callable[[str], str]
    verify_password: Callable[[str, str], bool]
    token_digest: Callable[[str], str]
    db_user: Callable[[int], dict[str, Any] | None]
    db_user_by_name: Callable[[str], dict[str, Any] | None]
    issue_session: Callable[[int], str]
    current_user: Callable[[], dict[str, Any]]
    public_user: Callable[[dict[str, Any]], dict[str, Any]]
    utc_now: Callable[[], str]
    db_execute: Callable[[str, tuple[Any, ...]], None]
    database_path: Callable[[], Path]
    login_failures: dict[tuple[str, str], list[float]]
    default_collection_name: str
    session_cookie: str
    session_days: int


def create_auth_router(deps: AuthRouteDependencies) -> APIRouter:
    router = APIRouter()

    def set_session_cookie(response: JSONResponse, token: str) -> None:
        response.set_cookie(
            deps.session_cookie,
            token,
            max_age=deps.session_days * 86400,
            httponly=True,
            secure=False,
            samesite="lax",
            path="/",
        )

    @router.post("/api/auth/login")
    async def auth_login(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _, username_key = deps.normalize_username(payload.get("username"))
        key = (request.client.host if request.client else "unknown", username_key)
        now = time.monotonic()
        recent = [
            entry for entry in deps.login_failures.get(key, [])
            if now - entry < 900
        ]
        if len(recent) >= 5:
            raise HTTPException(429, "登录失败次数过多，请 15 分钟后重试")
        user = deps.db_user_by_name(username_key)
        invalid = (
            not user
            or user["disabled"]
            or not deps.verify_password(
                str(payload.get("password") or ""),
                user["password_hash"],
            )
        )
        if invalid:
            recent.append(now)
            deps.login_failures[key] = recent
            raise HTTPException(401, "用户名或密码错误")
        deps.login_failures.pop(key, None)
        token = deps.issue_session(int(user["user_id"]))
        response = JSONResponse({"user": deps.public_user(user)})
        set_session_cookie(response, token)
        return response

    @router.post("/api/auth/register")
    async def auth_register(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        invite_token = str(payload.get("invite") or "").strip()
        display, username_key = deps.normalize_username(payload.get("username"))
        hashed_password = deps.password_hash(str(payload.get("password") or ""))
        now = deps.utc_now()
        try:
            with sqlite3.connect(deps.database_path()) as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                invite = db.execute(
                    "SELECT * FROM invites WHERE token_hash=? AND used_at IS NULL "
                    "AND revoked_at IS NULL AND expires_at>?",
                    (deps.token_digest(invite_token), now),
                ).fetchone()
                if not invite:
                    raise HTTPException(400, "邀请码无效或已过期")
                cursor = db.execute(
                    "INSERT INTO users(username,username_key,password_hash,role,created_at,updated_at) "
                    "VALUES(?,?,?,'user',?,?)",
                    (display, username_key, hashed_password, now, now),
                )
                user_id = int(cursor.lastrowid)
                db.execute(
                    "INSERT INTO favorite_collections(name,created_at,owner_id) VALUES(?,?,?)",
                    (deps.default_collection_name, now, user_id),
                )
                db.execute(
                    "UPDATE invites SET used_at=?,used_by=? WHERE invite_id=?",
                    (now, user_id, invite["invite_id"]),
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "用户名已存在") from exc
        user = deps.db_user(user_id) or {}
        token = deps.issue_session(int(user["user_id"]))
        response = JSONResponse({"user": deps.public_user(user)})
        set_session_cookie(response, token)
        return response

    @router.get("/api/auth/me")
    async def auth_me() -> dict[str, Any]:
        return {"user": deps.public_user(deps.current_user())}

    @router.post("/api/auth/logout")
    async def auth_logout(request: Request) -> JSONResponse:
        token = request.cookies.get(deps.session_cookie)
        if token:
            deps.db_execute(
                "DELETE FROM sessions WHERE session_hash=?",
                (deps.token_digest(token),),
            )
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(deps.session_cookie, path="/")
        return response

    @router.post("/api/auth/logout-all")
    async def auth_logout_all() -> JSONResponse:
        user = deps.current_user()
        deps.db_execute(
            "DELETE FROM sessions WHERE user_id=?",
            (user["user_id"],),
        )
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(deps.session_cookie, path="/")
        return response

    router.auth_login = auth_login  # type: ignore[attr-defined]
    router.auth_register = auth_register  # type: ignore[attr-defined]
    router.auth_me = auth_me  # type: ignore[attr-defined]
    router.auth_logout = auth_logout  # type: ignore[attr-defined]
    router.auth_logout_all = auth_logout_all  # type: ignore[attr-defined]
    return router
