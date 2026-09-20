from __future__ import annotations

import asyncio
from io import BytesIO
import gc
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.datastructures import Headers
from starlette.requests import Request

import mobile_server.app as app_module


class AccountProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.originals = {
            name: getattr(app_module, name)
            for name in ("DATABASE", "DATA_DIR", "AVATAR_DIR")
        }
        app_module.DATA_DIR = root / "data"
        app_module.DATABASE = app_module.DATA_DIR / "account.sqlite3"
        app_module.AVATAR_DIR = app_module.DATA_DIR / "avatars"
        app_module.init_database()
        self.alice = app_module.create_user_account("Alice", "alice-password-123")
        self.bob = app_module.create_user_account("Bob", "bob-password-12345")

    def tearDown(self) -> None:
        app_module.CURRENT_USER.set(None)
        app_module.CHAT_CONNECTIONS.clear()
        for name, value in self.originals.items():
            setattr(app_module, name, value)
        gc.collect()
        self.temporary.cleanup()

    def run_as(self, user: dict, awaitable):
        token = app_module.CURRENT_USER.set(app_module.db_user(user["user_id"]))
        try:
            return asyncio.run(awaitable)
        finally:
            app_module.CURRENT_USER.reset(token)

    @staticmethod
    def png_upload(size: tuple[int, int] = (640, 360)) -> UploadFile:
        body = BytesIO()
        Image.new("RGB", size, "#4c7fc4").save(body, "PNG")
        body.seek(0)
        return UploadFile(
            body, filename="avatar.png",
            headers=Headers({"content-type": "image/png"}),
        )

    def test_username_change_without_password_updates_chat_history(self) -> None:
        conversation_id = self.run_as(
            self.alice,
            app_module.create_friend_request({"username": "Bob"}),
        )["id"]
        accepted = self.run_as(self.bob, app_module.accept_friend_request(conversation_id))
        self.run_as(
            self.alice,
            app_module.send_chat_message(
                accepted["conversation_id"], {"body": "hello", "client_nonce": "rename"},
            ),
        )
        with patch.object(app_module, "emit_chat_event", new=AsyncMock()) as emit:
            result = self.run_as(self.alice, app_module.update_account_profile({
                "username": "Alicia",
            }))
        recipients, event = emit.await_args.args
        self.assertIn(self.alice["user_id"], recipients)
        self.assertIn(self.bob["user_id"], recipients)
        self.assertEqual(event["type"], "profile_updated")
        self.assertEqual(result["user"]["username"], "Alicia")
        with sqlite3.connect(app_module.DATABASE) as db:
            sender_name = db.execute(
                "SELECT sender_name FROM chat_messages WHERE sender_id=?",
                (self.alice["user_id"],),
            ).fetchone()[0]
        self.assertEqual(sender_name, "Alicia")
        overview = self.run_as(self.bob, app_module.chat_overview())
        self.assertEqual(overview["conversations"][0]["peer"]["username"], "Alicia")

        with self.assertRaises(HTTPException) as duplicate:
            self.run_as(self.alice, app_module.update_account_profile({
                "username": "bOB",
            }))
        self.assertEqual(duplicate.exception.status_code, 409)

    def test_avatar_is_square_versioned_private_and_deletable(self) -> None:
        result = self.run_as(
            self.alice,
            app_module.update_account_avatar(
                self.png_upload(),
                '{"x":0.25,"y":0,"width":0.5,"height":1}',
            ),
        )
        user = result["user"]
        self.assertEqual(user["avatar_version"], 1)
        self.assertEqual(user["avatar_url"], f"/api/users/{self.alice['user_id']}/avatar?v=1")
        path = app_module.avatar_path(self.alice["user_id"])
        with Image.open(path) as avatar:
            self.assertEqual(avatar.size, (256, 256))
            self.assertEqual(avatar.format, "WEBP")
        self.assertGreaterEqual(app_module.user_storage_bytes(self.alice["user_id"]), path.stat().st_size)

        response = self.run_as(
            self.bob,
            app_module.user_avatar(self.alice["user_id"], v=1),
        )
        self.assertEqual(response.headers["cache-control"], "private, max-age=31536000, immutable")
        with self.assertRaises(HTTPException) as stale:
            self.run_as(self.bob, app_module.user_avatar(self.alice["user_id"], v=2))
        self.assertEqual(stale.exception.status_code, 404)

        removed = self.run_as(self.alice, app_module.delete_account_avatar())
        self.assertIsNone(removed["user"]["avatar_url"])
        self.assertFalse(path.exists())

    def test_avatar_rejects_invalid_content(self) -> None:
        upload = UploadFile(
            BytesIO(b"not-an-image"), filename="avatar.png",
            headers=Headers({"content-type": "image/png"}),
        )
        with self.assertRaises(HTTPException) as invalid:
            self.run_as(self.alice, app_module.update_account_avatar(upload, None))
        self.assertEqual(invalid.exception.status_code, 400)

    def test_password_change_keeps_current_session_and_revokes_others(self) -> None:
        current = app_module.issue_session(self.alice["user_id"])
        other = app_module.issue_session(self.alice["user_id"])
        request = Request({
            "type": "http", "method": "POST", "path": "/api/account/password",
            "headers": [(b"cookie", f"{app_module.SESSION_COOKIE}={current}".encode())],
        })
        result = self.run_as(self.alice, app_module.update_account_password(request, {
            "current_password": "alice-password-123",
            "new_password": "alice-new-password-456",
        }))
        self.assertTrue(result["changed"])
        self.assertEqual(result["other_sessions_revoked"], 1)
        self.assertIsNotNone(app_module.session_user(current))
        self.assertIsNone(app_module.session_user(other))
        updated = app_module.db_user(self.alice["user_id"])
        self.assertTrue(app_module.verify_password("alice-new-password-456", updated["password_hash"]))


if __name__ == "__main__":
    unittest.main()
