from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import mobile_server.app as app_module


class MultiUserSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.originals = {name: getattr(app_module, name) for name in ("DATABASE", "COMFY_ROOT", "UPLOAD_DIR", "OUTPUT_DIR", "MOBILE_OUTPUT_DIR", "THUMBNAIL_DIR")}
        app_module.DATABASE = root / "security.sqlite3"
        app_module.COMFY_ROOT = root / "ComfyUI"
        app_module.UPLOAD_DIR = app_module.COMFY_ROOT / "input" / "mobile_uploads"
        app_module.OUTPUT_DIR = root / "output"
        app_module.MOBILE_OUTPUT_DIR = app_module.OUTPUT_DIR / "mobile"
        app_module.THUMBNAIL_DIR = root / "thumbnails"
        app_module.UPLOAD_DIR.mkdir(parents=True)
        app_module.MOBILE_OUTPUT_DIR.mkdir(parents=True)
        app_module.init_database()
        self.admin = app_module.create_admin_account("owner", "administrator-password")
        self.alice = app_module.create_user_account("alice", "alice-password-123")
        self.bob = app_module.create_user_account("bob", "bob-password-12345")

    def tearDown(self) -> None:
        app_module.CURRENT_USER.set(None)
        for name, value in self.originals.items():
            setattr(app_module, name, value)
        gc.collect()
        self.temporary.cleanup()

    def as_user(self, user: dict):
        return app_module.CURRENT_USER.set(user)

    def insert_group(self, group_id: str, owner_id: int) -> None:
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,storage_scope,owner_id) VALUES(?,?,?,?,?,'mobile',?)",
                (group_id, "qwen2511-modular-flux2", f"private {group_id}", "completed", app_module.utc_now(), owner_id),
            )

    def insert_private_image_group(self, group_id: str, owner_id: int) -> None:
        folder = app_module.MOBILE_OUTPUT_DIR / "cache-tests"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{group_id}.png"
        Image.new("RGB", (32, 32), "blue").save(path)
        image = {"filename": path.name, "subfolder": "mobile/cache-tests", "type": "output"}
        item = {"id": "item-1", "final": image, "stage1": None}
        app_module.db_execute(
            """INSERT INTO generation_groups(
                group_id,workflow_key,prompt_text,status,submitted_at,completed_at,
                outputs_json,parameters_json,items_json,storage_scope,owner_id
            ) VALUES(?,?,?,?,?,?,?,?,?,'mobile',?)""",
            (
                group_id, "qwen2511-modular-flux2", "private cached image", "completed",
                app_module.utc_now(), app_module.utc_now(), json.dumps({"final": [image], "stage1": []}),
                "{}", json.dumps([item]), owner_id,
            ),
        )

    def test_unauthenticated_api_is_rejected(self) -> None:
        with TestClient(app_module.app, base_url="https://testserver") as client:
            response = client.get("/api/gallery")
        self.assertEqual(response.status_code, 401)

    def test_account_storage_scan_runs_off_the_event_loop(self) -> None:
        token = self.as_user(self.alice)
        try:
            worker = AsyncMock(return_value=321)
            with patch.object(app_module.asyncio, "to_thread", worker):
                result = asyncio.run(app_module.account_storage())
            worker.assert_awaited_once_with(
                app_module.user_storage_bytes, int(self.alice["user_id"]),
            )
            self.assertEqual(result["used_bytes"], 321)
            self.assertEqual(result["quota_bytes"], app_module.USER_QUOTA_BYTES)
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_storage_usage_is_shared_between_users_until_invalidated(self) -> None:
        app_module.invalidate_storage_usage_cache()
        expected = {
            int(self.alice["user_id"]): 123,
            int(self.bob["user_id"]): 456,
        }
        with patch.object(app_module, "calculate_storage_usage", return_value=expected) as loader:
            self.assertEqual(app_module.user_storage_bytes(int(self.alice["user_id"])), 123)
            self.assertEqual(app_module.user_storage_bytes(int(self.bob["user_id"])), 456)
            self.assertEqual(loader.call_count, 1)
            app_module.invalidate_storage_usage_cache()
            self.assertEqual(app_module.user_storage_bytes(int(self.alice["user_id"])), 123)
            self.assertEqual(loader.call_count, 2)

    def test_cross_origin_mutation_is_rejected(self) -> None:
        session = app_module.issue_session(self.alice["user_id"])
        with TestClient(app_module.app, base_url="https://testserver") as client:
            client.cookies.set(app_module.SESSION_COOKIE, session)
            response = client.post("/api/auth/logout", headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(response.status_code, 403)

    def test_group_and_gallery_are_owner_scoped(self) -> None:
        self.insert_group("alice-group", self.alice["user_id"])
        self.insert_group("bob-group", self.bob["user_id"])
        token = self.as_user(self.alice)
        try:
            self.assertIsNotNone(app_module.db_group("alice-group"))
            self.assertIsNone(app_module.db_group("bob-group"))
            gallery = asyncio.run(app_module.gallery())
            self.assertEqual([item["id"] for item in gallery], ["alice-group"])
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_active_gallery_placeholders_are_owner_scoped(self) -> None:
        self.insert_group("alice-active", self.alice["user_id"])
        self.insert_group("bob-active", self.bob["user_id"])
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute("UPDATE generation_groups SET status='queued' WHERE group_id IN ('alice-active','bob-active')")
        token = self.as_user(self.alice)
        try:
            gallery = asyncio.run(app_module.gallery(include_active=True))
            self.assertEqual([item["id"] for item in gallery], ["alice-active"])
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_cross_user_http_lookup_returns_not_found(self) -> None:
        self.insert_group("bob-secret", self.bob["user_id"])
        session = app_module.issue_session(self.alice["user_id"])
        with TestClient(app_module.app, base_url="https://testserver") as client:
            client.cookies.set(app_module.SESSION_COOKIE, session)
            response = client.get("/api/jobs/bob-secret")
        self.assertEqual(response.status_code, 404)

    def test_private_preview_cache_is_cookie_isolated_and_still_owner_scoped(self) -> None:
        self.insert_private_image_group("alice-cache", self.alice["user_id"])
        alice_session = app_module.issue_session(self.alice["user_id"])
        bob_session = app_module.issue_session(self.bob["user_id"])
        url = "/api/images/alice-cache/items/item-1/final?preview=512"
        with TestClient(app_module.app, base_url="https://testserver") as client:
            client.cookies.set(app_module.SESSION_COOKIE, alice_session)
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, max-age=604800, stale-while-revalidate=86400")
        self.assertEqual(response.headers["vary"], "Cookie")
        with TestClient(app_module.app, base_url="https://testserver") as client:
            client.cookies.set(app_module.SESSION_COOKIE, bob_session)
            response = client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_device_task_status_is_bounded_and_owner_scoped(self) -> None:
        self.insert_group("alice-complete", self.alice["user_id"])
        self.insert_group("bob-complete", self.bob["user_id"])
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO upscale_jobs(upscale_id,parent_group_id,parent_item_id,status,submitted_at,completed_at,owner_id) "
                "VALUES('alice-upscale','alice-complete','item-1','completed',?,?,?)",
                (app_module.utc_now(), app_module.utc_now(), self.alice["user_id"]),
            )
            db.execute(
                "INSERT INTO detail_jobs(detail_id,parent_group_id,parent_item_id,status,submitted_at,completed_at,error_message,owner_id) "
                "VALUES('alice-detail','alice-complete','item-1','failed',?,?,?,?)",
                (app_module.utc_now(), app_module.utc_now(), "detail failed", self.alice["user_id"]),
            )

        token = self.as_user(self.alice)
        try:
            result = asyncio.run(app_module.device_task_status({"tasks": [
                {"type": "generation", "id": "alice-complete"},
                {"type": "generation", "id": "bob-complete"},
                {"type": "upscale", "id": "alice-upscale"},
                {"type": "detail", "id": "alice-detail"},
            ]}))
            with self.assertRaises(HTTPException) as too_many:
                asyncio.run(app_module.device_task_status({"tasks": [
                    {"type": "generation", "id": f"group-{index}"} for index in range(51)
                ]}))
            with self.assertRaises(HTTPException) as invalid:
                asyncio.run(app_module.device_task_status({"tasks": [{"type": "prompt_tool", "id": "secret"}]}))
        finally:
            app_module.CURRENT_USER.reset(token)

        by_key = {(item["type"], item["id"]): item for item in result["tasks"]}
        self.assertTrue(by_key[("generation", "alice-complete")]["exists"])
        self.assertFalse(by_key[("generation", "bob-complete")]["exists"])
        self.assertEqual(by_key[("upscale", "alice-upscale")]["status"], "completed")
        self.assertEqual(by_key[("detail", "alice-detail")]["status"], "failed")
        self.assertEqual(by_key[("detail", "alice-detail")]["error"], "detail failed")
        self.assertEqual(too_many.exception.status_code, 400)
        self.assertEqual(invalid.exception.status_code, 400)

    def test_invite_is_single_use(self) -> None:
        token = self.as_user(self.admin)
        try:
            invite = asyncio.run(app_module.admin_create_invite({"days": 7}))
        finally:
            app_module.CURRENT_USER.reset(token)
        response = asyncio.run(app_module.auth_register({"invite": invite["token"], "username": "friend", "password": "friend-password-123"}))
        self.assertEqual(response.status_code, 200)
        with self.assertRaises(HTTPException) as reused:
            asyncio.run(app_module.auth_register({"invite": invite["token"], "username": "friend2", "password": "friend-password-456"}))
        self.assertEqual(reused.exception.status_code, 400)

    def test_system_template_edit_creates_private_copy(self) -> None:
        token = self.as_user(self.alice)
        try:
            system = next(item for item in app_module.all_prompt_templates(app_module.PROMPT_TEMPLATE_SCOPE) if item["system"])
            copied = asyncio.run(app_module.update_prompt_template(system["id"], {"name": "我的副本", "text": "私人内容"}))
            self.assertFalse(copied["system"])
            self.assertNotEqual(copied["id"], system["id"])
        finally:
            app_module.CURRENT_USER.reset(token)
        token = self.as_user(self.bob)
        try:
            self.assertNotIn("我的副本", {item["name"] for item in app_module.all_prompt_templates(app_module.PROMPT_TEMPLATE_SCOPE)})
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_three_active_task_limit(self) -> None:
        with sqlite3.connect(app_module.DATABASE) as db:
            for index in range(3):
                db.execute("INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at) VALUES('generation',?,?,'waiting',?)", (f"active-{index}", self.alice["user_id"], app_module.utc_now()))
        token = self.as_user(self.alice)
        try:
            with self.assertRaises(HTTPException) as raised:
                app_module.enforce_user_capacity()
            self.assertEqual(raised.exception.status_code, 429)
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_admin_activity_is_content_free(self) -> None:
        self.insert_group("private-group", self.alice["user_id"])
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute("UPDATE generation_groups SET status='running' WHERE group_id='private-group'")
        token = self.as_user(self.admin)
        try:
            rows = asyncio.run(app_module.admin_activity())
        finally:
            app_module.CURRENT_USER.reset(token)
        encoded = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("private private-group", encoded)
        self.assertNotIn("prompt", encoded)
        self.assertNotIn("output", encoded)

    def test_tip_management_requires_admin(self) -> None:
        token = self.as_user(self.alice)
        try:
            with self.assertRaises(HTTPException) as denied:
                asyncio.run(app_module.create_usage_tip({
                    "placement": "create_prompt", "workflow": "krea-identity-edit",
                    "title": "不能创建", "body": "普通用户无权管理提示。",
                }))
            self.assertEqual(denied.exception.status_code, 403)
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_first_admin_claims_legacy_history_but_templates_stay_shared(self) -> None:
        legacy_database = Path(self.temporary.name) / "legacy.sqlite3"
        current_database = app_module.DATABASE
        app_module.DATABASE = legacy_database
        try:
            app_module.init_database()
            with sqlite3.connect(legacy_database) as db:
                db.execute(
                    "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,storage_scope) VALUES(?,?,?,?,?,'mobile')",
                    ("legacy-private", "qwen2511-modular-flux2", "old private prompt", "completed", app_module.utc_now()),
                )
            owner = app_module.create_admin_account("legacy-owner", "legacy-owner-password")
            with sqlite3.connect(legacy_database) as db:
                group_owner = db.execute("SELECT owner_id FROM generation_groups WHERE group_id='legacy-private'").fetchone()[0]
                template_owners = {row[0] for row in db.execute("SELECT owner_id FROM prompt_templates")}
            self.assertEqual(group_owner, owner["user_id"])
            self.assertEqual(template_owners, {None})
        finally:
            app_module.DATABASE = current_database


if __name__ == "__main__":
    unittest.main()
