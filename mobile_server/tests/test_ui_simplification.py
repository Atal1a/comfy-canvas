from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import mobile_server.app as app_module
from mobile_server.config import DEFAULT_WORKFLOWS


STATIC = Path(__file__).resolve().parents[1] / "static"


class UiSimplificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_database = app_module.DATABASE
        app_module.DATABASE = Path(self.temporary.name) / "ui.sqlite3"
        app_module.init_database()
        self.admin = app_module.create_admin_account("owner", "administrator-password")

    def tearDown(self) -> None:
        app_module.CURRENT_USER.set(None)
        app_module.DATABASE = self.original_database
        gc.collect()
        self.temporary.cleanup()

    def test_primary_workflows_include_minimax_h3(self) -> None:
        with patch.object(app_module, "ENABLED_WORKFLOW_KEYS", frozenset(DEFAULT_WORKFLOWS)):
            descriptors = asyncio.run(app_module.workflows())
        self.assertEqual(
            [item["key"] for item in descriptors],
            ["krea-turbo", "krea-identity-edit", "minimax-h3", "qwen2511-modular-flux2"],
        )
        with self.assertRaises(HTTPException) as retired:
            asyncio.run(app_module.submit_job("removed-workflow", "old", "{}", None, None, None))
        self.assertEqual(retired.exception.status_code, 400)

    def test_admin_can_generate_up_to_eight_images(self) -> None:
        spec = app_module.get_spec("qwen2511-modular-flux2")
        values = app_module.workflow_descriptor(spec)["defaults"]
        regular = app_module.create_user_account("regular", "regular-user-password")
        self.assertEqual(app_module.public_user(regular)["max_generation_count"], 4)
        self.assertEqual(app_module.public_user(self.admin)["max_generation_count"], 8)
        for user, allowed in ((regular, 4), (self.admin, 8)):
            token = app_module.CURRENT_USER.set(user)
            try:
                values["count"] = allowed
                self.assertEqual(app_module.normalize_settings(spec, json.dumps(values))["count"], allowed)
                values["count"] = allowed + 1
                with self.assertRaises(HTTPException):
                    app_module.normalize_settings(spec, json.dumps(values))
            finally:
                app_module.CURRENT_USER.reset(token)

    def test_default_tips_and_admin_crud(self) -> None:
        defaults = app_module.all_usage_tips()
        self.assertEqual(len(defaults), 8)
        self.assertFalse(any(item["placement"] == "pre_upscale" for item in defaults))
        self.assertIn("4K", next(item["body"] for item in defaults if item["placement"] == "flux_upscale"))
        token = app_module.CURRENT_USER.set(self.admin)
        try:
            created = asyncio.run(app_module.create_usage_tip({
                "placement": "create_parameters", "workflow": "krea-identity-edit",
                "title": "测试提示", "body": "只用于验证。", "sort_order": 20, "enabled": True,
            }))
            qwen = asyncio.run(app_module.create_usage_tip({
                "placement": "create_parameters", "workflow": "qwen2511-modular-flux2",
                "title": "Qwen 参数", "body": "仅用于验证。", "sort_order": 21, "enabled": True,
            }))
            updated = asyncio.run(app_module.update_usage_tip(created["id"], {"enabled": False}))
            self.assertFalse(updated["enabled"])
            self.assertNotIn(created["id"], {item["id"] for item in app_module.all_usage_tips()})
            self.assertIn(created["id"], {item["id"] for item in app_module.all_usage_tips(True)})
            asyncio.run(app_module.delete_usage_tip(created["id"]))
            asyncio.run(app_module.delete_usage_tip(qwen["id"]))
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_help_migration_and_lora_metadata_crud(self) -> None:
        migrated = app_module.all_help_entries()
        self.assertEqual(len([item for item in migrated if item["topic"].startswith("legacy:")]), 8)
        self.assertTrue(any(item["scope"] == "page" for item in migrated))
        self.assertIn("create_step_1", app_module.page_help_entries("qwen2511-modular-flux2"))
        self.assertIn("flux_upscale", app_module.page_help_entries())
        h3_help = app_module.page_help_entries("minimax-h3")
        self.assertIn("官方推荐", h3_help["create_step_1"]["body"])
        self.assertIn("integrated_multimodal_description", h3_help["create_step_1"]["body"])
        self.assertIn("0.6 MP", h3_help["create_step_2"]["body"])
        self.assertNotIn("管理员", h3_help["create_step_2"]["body"])
        self.assertNotIn("普通用户", h3_help["create_step_2"]["body"])
        token = app_module.CURRENT_USER.set(self.admin)
        try:
            created = asyncio.run(app_module.create_help_entry({
                "scope": "parameter", "workflow": "krea-identity-edit", "topic": "steps",
                "title": "Steps", "body": "Higher values take longer.", "enabled": True,
            }))
            self.assertEqual(created["topic"], "steps")
            long_description = "长说明段落，可包含完整的用途、触发词和注意事项。\n" * 300
            updated = asyncio.run(app_module.update_lora_metadata("krea2", "example.safetensors", {
                "description": long_description, "recommended_min": 0.6,
                "recommended_max": 1.1, "enabled": True,
            }))
            self.assertGreater(len(long_description), 600)
            self.assertEqual(updated["description"], long_description.strip())
            self.assertEqual(updated["recommended_min"], 0.6)
            self.assertEqual(updated["recommended_max"], 1.1)
            asyncio.run(app_module.delete_help_entry(created["id"]))
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_lora_favorites_are_account_scoped_and_included_in_help(self) -> None:
        regular = app_module.create_user_account("favorite-user", "favorite-user-password")
        name = "styles/favorite.safetensors"
        catalog = [("krea2", name, Path(self.temporary.name) / "favorite.safetensors")]
        with (
            patch.object(app_module, "synchronize_lora_registry", return_value=[]),
            patch.object(app_module, "lora_catalog_entries", return_value=catalog),
        ):
            token = app_module.CURRENT_USER.set(self.admin)
            try:
                first = asyncio.run(app_module.favorite_lora("krea2", name))
                second = asyncio.run(app_module.favorite_lora("krea2", name))
                help_payload = asyncio.run(app_module.help_content("krea-identity-edit"))
                self.assertTrue(first["favorite"])
                self.assertEqual(second, first)
                self.assertEqual(help_payload["lora_favorites"], [name])
                with self.assertRaises(HTTPException) as missing:
                    asyncio.run(app_module.favorite_lora("krea2", "missing.safetensors"))
                self.assertEqual(missing.exception.status_code, 404)
            finally:
                app_module.CURRENT_USER.reset(token)

            token = app_module.CURRENT_USER.set(regular)
            try:
                self.assertEqual(asyncio.run(app_module.help_content("krea-identity-edit"))["lora_favorites"], [])
                asyncio.run(app_module.favorite_lora("krea2", name))
                self.assertEqual(asyncio.run(app_module.help_content("krea-identity-edit"))["lora_favorites"], [name])
            finally:
                app_module.CURRENT_USER.reset(token)

            token = app_module.CURRENT_USER.set(self.admin)
            try:
                result = asyncio.run(app_module.unfavorite_lora("krea2", name))
                self.assertFalse(result["favorite"])
                self.assertEqual(asyncio.run(app_module.help_content("krea-identity-edit"))["lora_favorites"], [])
            finally:
                app_module.CURRENT_USER.reset(token)

        with sqlite3.connect(app_module.DATABASE) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM lora_favorites").fetchone()[0], 1)

    def test_admin_manages_global_lora_categories(self) -> None:
        self.assertEqual([item["name"] for item in app_module.lora_category_rows()], ["内容增强", "风格", "调整"])
        token = app_module.CURRENT_USER.set(self.admin)
        try:
            created = asyncio.run(app_module.create_lora_category({"name": "角色"}))
            updated = asyncio.run(app_module.update_lora_metadata("krea2", "categorized.safetensors", {
                "description": "角色 LoRA", "category_id": created["category_id"], "enabled": True,
            }))
            self.assertEqual(updated["category_id"], created["category_id"])
            renamed = asyncio.run(app_module.update_lora_category(created["category_id"], {"name": "人物"}))
            self.assertEqual(renamed["name"], "人物")
            deleted = asyncio.run(app_module.delete_lora_category(created["category_id"]))
            self.assertEqual(deleted["uncategorized_loras"], 1)
            metadata = next(item for item in app_module.lora_metadata_rows("krea2", True) if item["name"] == "categorized.safetensors")
            self.assertIsNone(metadata["category_id"])
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_admin_can_identify_and_delete_only_stale_lora_metadata(self) -> None:
        current_name = "current.safetensors"
        stale_name = "missing.safetensors"
        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            for name in (current_name, stale_name):
                db.execute(
                    "INSERT INTO lora_metadata(lora_name,family,description,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (name, "krea2", f"Description for {name}", 1, now, now),
                )
            db.execute(
                "INSERT INTO lora_favorites(owner_id,family,lora_name,created_at) VALUES(?,?,?,?)",
                (self.admin["user_id"], "krea2", stale_name, now),
            )
        catalog = [("krea2", current_name, Path(self.temporary.name) / current_name)]
        token = app_module.CURRENT_USER.set(self.admin)
        try:
            with (
                patch.object(app_module, "synchronize_lora_registry", return_value=[]),
                patch.object(app_module, "enabled_workflows", return_value=[app_module.get_spec("krea-identity-edit")]),
                patch.object(app_module, "lora_options", return_value=[current_name]),
            ):
                items = asyncio.run(app_module.admin_lora_metadata())
            availability = {item["name"]: item["available"] for item in items}
            self.assertEqual(availability, {current_name: True, stale_name: False})

            with (
                patch.object(app_module, "synchronize_lora_registry", return_value=[]),
                patch.object(app_module, "lora_catalog_entries", return_value=catalog),
            ):
                with self.assertRaises(HTTPException) as current:
                    asyncio.run(app_module.delete_stale_lora_metadata("krea2", current_name))
                self.assertEqual(current.exception.status_code, 409)
                deleted = asyncio.run(app_module.delete_stale_lora_metadata("krea2", stale_name))
            self.assertTrue(deleted["deleted"])
        finally:
            app_module.CURRENT_USER.reset(token)

        with sqlite3.connect(app_module.DATABASE) as db:
            names = {row[0] for row in db.execute("SELECT lora_name FROM lora_metadata")}
            favorites = db.execute("SELECT COUNT(*) FROM lora_favorites").fetchone()[0]
        self.assertEqual(names, {current_name})
        self.assertEqual(favorites, 0)

    def test_lora_file_rename_migrates_metadata_history_and_draft_aliases(self) -> None:
        lora_root = Path(self.temporary.name) / "loras"
        lora_root.mkdir()
        old_path = lora_root / "portrait-old.safetensors"
        old_path.write_bytes((b"stable-lora-content-" * 32768) + b"tail")
        old_name = "styles/portrait-old.safetensors"
        middle_name = "styles/portrait-middle.safetensors"
        new_name = "renamed/portrait-final.safetensors"
        with patch.object(app_module, "lora_catalog_entries", return_value=[("krea2", old_name, old_path)]):
            self.assertEqual(app_module.synchronize_lora_registry(True), [])
        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO lora_metadata(lora_name,family,description,recommended_min,recommended_max,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (old_name, "krea2", "Portrait style", 0.6, 1.1, 1, now, now),
            )
            db.execute(
                "INSERT INTO lora_favorites(owner_id,family,lora_name,created_at) VALUES(?,?,?,?)",
                (self.admin["user_id"], "krea2", old_name, now),
            )
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,parameters_json,storage_scope) VALUES(?,?,?,?,?,?,'mobile')",
                ("lora-history", "krea-identity-edit", "prompt", "completed", now, json.dumps({"loras": [{"name": old_name, "weight": 0.8}]})),
            )
            db.execute(
                "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,parameters_json,storage_scope,group_id) VALUES(?,?,?,?,?,?,'mobile',?)",
                ("lora-child", "krea-identity-edit", "prompt", "completed", now, json.dumps({"loras": [{"name": old_name, "weight": 0.8}]}), "lora-history"),
            )
            db.execute(
                "INSERT INTO chat_attachments(attachment_id,message_id,kind,origin_owner_id,origin_group_id,manifest_json,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("lora-share", 999, "generation", self.admin["user_id"], "lora-history", "{}", json.dumps({"parameters": {"loras": [{"name": old_name, "weight": 0.8}]}}), now),
            )
        middle_path = lora_root / "portrait-middle.safetensors"
        old_path.rename(middle_path)
        with patch.object(app_module, "lora_catalog_entries", return_value=[("krea2", middle_name, middle_path)]):
            migrated = app_module.synchronize_lora_registry(True)
        self.assertEqual(migrated, [{"family": "krea2", "old_name": old_name, "new_name": middle_name}])
        final_path = lora_root / "portrait-final.safetensors"
        middle_path.rename(final_path)
        with patch.object(app_module, "lora_catalog_entries", return_value=[("krea2", new_name, final_path)]):
            app_module.synchronize_lora_registry(True)
        metadata = app_module.lora_metadata_rows("krea2", True)
        self.assertEqual([(item["name"], item["description"]) for item in metadata], [(new_name, "Portrait style")])
        self.assertEqual((metadata[0]["recommended_min"], metadata[0]["recommended_max"], metadata[0]["enabled"]), (0.6, 1.1, True))
        parameters = json.loads(app_module.unrestricted_group("lora-history")["parameters_json"])
        self.assertEqual(parameters["loras"][0]["name"], new_name)
        self.assertEqual(app_module.db_job("lora-child")["parameters_json"], json.dumps(parameters))
        with sqlite3.connect(app_module.DATABASE) as db:
            snapshot = json.loads(db.execute(
                "SELECT snapshot_json FROM chat_attachments WHERE attachment_id='lora-share'",
            ).fetchone()[0])
        self.assertEqual(snapshot["parameters"]["loras"][0]["name"], new_name)
        aliases = app_module.lora_alias_map("krea2")
        self.assertEqual(aliases[old_name], new_name)
        self.assertEqual(aliases[middle_name], new_name)
        with sqlite3.connect(app_module.DATABASE) as db:
            favorite_name = db.execute(
                "SELECT lora_name FROM lora_favorites WHERE owner_id=? AND family='krea2'",
                (self.admin["user_id"],),
            ).fetchone()[0]
        self.assertEqual(favorite_name, new_name)

    def test_admin_lora_rename_moves_file_and_migrates_all_managed_families(self) -> None:
        lora_root = Path(self.temporary.name) / "loras" / "styles"
        lora_root.mkdir(parents=True)
        source = lora_root / "portrait-old.safetensors"
        source.write_bytes((b"rename-through-admin-" * 32768) + b"tail")
        old_name = "styles/portrait-old.safetensors"
        new_name = "styles/portrait-final.safetensors"

        def catalog():
            current = next(lora_root.glob("*.safetensors"))
            name = f"styles/{current.name}"
            return [("krea2", name, current), ("qwen2511", name, current)]

        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            for family in ("krea2", "qwen2511"):
                db.execute(
                    "INSERT INTO lora_metadata(lora_name,family,description,recommended_min,recommended_max,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (old_name, family, f"{family} portrait", 0.55, 0.95, 1, now, now),
                )
            db.execute(
                "INSERT INTO lora_metadata(lora_name,family,description,recommended_min,recommended_max,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (new_name, "krea2", "stale target metadata", None, None, 0, now, now),
            )
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,parameters_json,storage_scope,owner_id) VALUES(?,?,?,?,?,?,'mobile',?)",
                ("rename-history", "krea-identity-edit", "prompt", "completed", now, json.dumps({"loras": [{"name": old_name, "weight": 0.8}]}), self.admin["user_id"]),
            )
        with patch.object(app_module, "lora_catalog_entries", side_effect=catalog):
            result = app_module.rename_managed_lora_file("krea2", old_name, "portrait-final")
        self.assertFalse(source.exists())
        self.assertTrue((lora_root / "portrait-final.safetensors").is_file())
        self.assertEqual(result["new_name"], new_name)
        self.assertEqual({item["family"] for item in result["migrations"]}, {"krea2", "qwen2511"})
        self.assertEqual(
            {item["name"] for item in app_module.lora_metadata_rows(None, True)}, {new_name},
        )
        descriptions = {item["family"]: item["description"] for item in app_module.lora_metadata_rows(None, True)}
        self.assertEqual(descriptions, {"krea2": "krea2 portrait", "qwen2511": "qwen2511 portrait"})
        parameters = json.loads(app_module.unrestricted_group("rename-history")["parameters_json"])
        self.assertEqual(parameters["loras"][0]["name"], new_name)
        self.assertEqual(app_module.lora_alias_map("krea2")[old_name], new_name)
        self.assertEqual(app_module.lora_alias_map("qwen2511")[old_name], new_name)

    def test_admin_lora_rename_blocks_only_referencing_active_task(self) -> None:
        lora_root = Path(self.temporary.name) / "loras"
        lora_root.mkdir()
        source = lora_root / "active-old.safetensors"
        source.write_bytes((b"active-lora-" * 32768) + b"tail")
        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,parameters_json,storage_scope,owner_id) VALUES(?,?,?,?,?,?,'mobile',?)",
                ("active-group", "krea-identity-edit", "prompt", "queued", now, json.dumps({"loras": [{"name": "active-old.safetensors", "weight": 0.8}]}), self.admin["user_id"]),
            )
            db.execute(
                "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at) VALUES('generation',?,?,?,?)",
                ("active-group", self.admin["user_id"], "waiting", now),
            )
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,parameters_json,storage_scope,owner_id) VALUES(?,?,?,?,?,?,'mobile',?)",
                ("unrelated-group", "krea-identity-edit", "prompt", "queued", now, json.dumps({"loras": [{"name": "another.safetensors", "weight": 0.8}]}), self.admin["user_id"]),
            )
            db.execute(
                "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at) VALUES('generation',?,?,?,?)",
                ("unrelated-group", self.admin["user_id"], "waiting", now),
            )

        def catalog():
            current = next(lora_root.glob("*.safetensors"))
            return [("krea2", current.name, current)]

        with patch.object(app_module, "lora_catalog_entries", side_effect=catalog):
            with self.assertRaises(HTTPException) as blocked:
                app_module.rename_managed_lora_file("krea2", "active-old.safetensors", "active-new")
            self.assertEqual(blocked.exception.status_code, 409)
            self.assertTrue(source.is_file())
            with sqlite3.connect(app_module.DATABASE) as db:
                db.execute("UPDATE task_queue SET state='completed' WHERE record_id='active-group'")
            result = app_module.rename_managed_lora_file("krea2", "active-old.safetensors", "active-new")
            case_result = app_module.rename_managed_lora_file("krea2", "active-new.safetensors", "ACTIVE-NEW")
        self.assertEqual(result["new_name"], "active-new.safetensors")
        self.assertEqual(case_result["new_name"], "ACTIVE-NEW.safetensors")
        self.assertTrue((lora_root / "ACTIVE-NEW.safetensors").is_file())

    def test_admin_lora_rename_rolls_back_when_classification_changes(self) -> None:
        lora_root = Path(self.temporary.name) / "loras"
        lora_root.mkdir()
        source = lora_root / "classified-old.safetensors"
        source.write_bytes((b"classification-" * 32768) + b"tail")

        def catalog():
            old = lora_root / "classified-old.safetensors"
            return [("krea2", old.name, old)] if old.exists() else []

        with patch.object(app_module, "lora_catalog_entries", side_effect=catalog):
            with self.assertRaises(HTTPException) as rejected:
                app_module.rename_managed_lora_file("krea2", source.name, "no-longer-classified")
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertTrue(source.is_file())
        self.assertFalse((lora_root / "no-longer-classified.safetensors").exists())
        collision = lora_root / "collision.safetensors"
        collision.write_bytes(b"different")
        with patch.object(app_module, "lora_catalog_entries", return_value=[("krea2", source.name, source)]):
            with self.assertRaises(HTTPException) as conflict:
                app_module.rename_managed_lora_file("krea2", source.name, "collision")
        self.assertEqual(conflict.exception.status_code, 409)

    def test_lora_rename_endpoint_requires_admin(self) -> None:
        regular = app_module.create_user_account("rename-viewer", "regular-user-password")
        token = app_module.CURRENT_USER.set(regular)
        try:
            with self.assertRaises(HTTPException) as denied:
                asyncio.run(app_module.rename_lora_file("krea2", "example.safetensors", {"name": "renamed"}))
        finally:
            app_module.CURRENT_USER.reset(token)
        self.assertEqual(denied.exception.status_code, 403)

    def test_lora_rename_filename_validation(self) -> None:
        self.assertEqual(app_module.normalized_lora_filename("新名字.safetensors"), "新名字.safetensors")
        for invalid in ("", " name", "name ", "../escape", "folder/name", "bad:name", "CON", "LPT1.txt", "trailing."):
            with self.subTest(invalid=invalid), self.assertRaises(HTTPException):
                app_module.normalized_lora_filename(invalid)

    def test_usage_tip_upgrade_adds_identity_tips_without_overwriting_admin_edits(self) -> None:
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute("DELETE FROM usage_tips WHERE workflow_key='krea-identity-edit'")
            db.execute(
                "UPDATE usage_tips SET body='管理员自定义内容' "
                "WHERE placement='create_input' AND workflow_key='qwen2511-modular-flux2'"
            )
            db.execute(
                "UPDATE app_metadata SET value='2' WHERE key='usage_tip_seed_version'"
            )

        app_module.init_database()

        tips = app_module.all_usage_tips()
        identity_tips = [item for item in tips if item["workflow"] == "krea-identity-edit"]
        self.assertEqual(len(identity_tips), 4)
        self.assertEqual(
            next(
                item["body"]
                for item in tips
                if item["placement"] == "create_input"
                and item["workflow"] == "qwen2511-modular-flux2"
            ),
            "管理员自定义内容",
        )

    def test_mobile_collection_update_does_not_reload_history(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        start = script.index("async function applyPicker()")
        end = script.index("function renderStorage()", start)
        self.assertNotIn("resetHistory", script[start:end])
        self.assertIn("restorePickerPosition", script[start:end])

    def test_tips_and_desktop_workspace_are_present(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        interface = (STATIC / "interface.css").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="view-tips"', html)
        self.assertNotIn('id="pre-upscale-option"', html)
        self.assertNotIn('实验性高质量', html)
        self.assertNotIn('3K 高质量', html)
        self.assertNotIn('4K 最高质量', html)
        self.assertNotIn('3B（快速）', script)
        self.assertNotIn('7B（高质量）', script)
        self.assertIn('id="krea-size-presets"', html)
        self.assertIn('id="krea-size-summary"', html)
        self.assertEqual(html.count('data-ratio='), 8)
        self.assertNotIn('data-width="1024"', html)
        self.assertIn("const KREA_RATIOS={'1:1':[1,1]", script)
        self.assertIn("function normalizeKreaPair(width,height,pixelBudget=null)", script)
        self.assertIn("function detectKreaRatio(width,height)", script)
        self.assertIn("function applyKreaRatio(key)", script)
        self.assertIn("function handleKreaDimensionChange(event)", script)
        self.assertIn("const KREA_IDENTITY_MODE_PRESETS=", script)
        self.assertIn("function applyKreaIdentityModePreset(mode)", script)
        self.assertIn("identity:{stage1_steps:12,stage1_cfg:1,stage1_denoise:1", script)
        self.assertIn("ref_boost:4,grounding_px:1024", script)
        self.assertIn("if(name==='ref_boost'){input.min='0';input.max='10'}", script)
        self.assertNotIn("face_swap:{krea_model:'official_turbo'", script)
        self.assertNotIn("function startFaceSwap(reference)", script)
        self.assertIn("function isKreaWorkflow(key)", script)
        self.assertNotIn("function openFaceReferencePicker()", script)
        self.assertIn("dual_reference", script)
        self.assertIn("reference_source_group_id", script)
        self.assertIn("job.parameters?.reference_mode!=='identity'", script)
        self.assertNotIn('id="face-reference-section"', html)
        self.assertNotIn('id="face-reference-picker"', html)
        self.assertIn('id="reference-image-panel"', html)
        self.assertIn('id="krea-reference-mode-entry"', html)
        self.assertIn('name="reference_image"', html)
        self.assertIn("Math.sqrt(area*ratioWidth/ratioHeight)", script)
        self.assertIn("Math.round(Number(value)/8)*8", script)
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr))", css)
        self.assertNotIn('data-tip-placement=', html)
        self.assertNotIn('ⓘ', html)
        self.assertIn('id="step-1-help"', html)
        self.assertIn('id="step-2-help"', html)
        self.assertIn('id="flux-help"', html)
        self.assertIn('id="lora-guide-open"', html)
        self.assertIn("function updateLoraGuideWeightState", script)
        self.assertIn("capacity.remaining?`已加入参数，请填写本次权重；还可选择 ${capacity.remaining} 个 LoRA。`", script)
        self.assertNotIn("已加入参数，请关闭说明并填写权重。", script)
        self.assertIn('id="page-help-form"', html)
        self.assertIn('id="page-help-editor-shell"', html)
        self.assertIn('id="page-help-stage-count"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn('class="admin-help-workspace admin-page-help-workspace"', html)
        self.assertIn('id="lora-meta-editor-shell"', html)
        self.assertIn('id="lora-meta-result-count"', html)
        self.assertIn('id="lora-meta-close"', html)
        self.assertIn('id="lora-meta-rename-open"', html)
        self.assertIn('id="lora-meta-rename-editor"', html)
        self.assertIn('<details class="lora-category-manager">', html)
        self.assertIn('id="lora-category-count"', html)
        self.assertIn('class="lora-meta-section"', html)
        self.assertIn('<span>资料状态</span>', html)
        self.assertEqual(html.count('id="lora-meta-form"'), 1)
        self.assertIn("comfyCanvasPageHelpSeenV2", script)
        self.assertNotIn("comfyCanvasHelpSeenV1", script)
        self.assertIn("function confirmLoraMetaDiscard()", script)
        self.assertIn("function openLoraMetaEditor()", script)
        self.assertIn("function openPageHelpEditor()", script)
        self.assertIn("function closePageHelpEditor(force=false)", script)
        self.assertIn("function switchAdminHelpTab(button)", script)
        self.assertIn("confirmPageHelpDiscard()", script)
        self.assertIn("root.scrollTop=scrollTop", script)
        self.assertIn("lora-manager-row-badges", script)
        self.assertIn("height:clamp(480px,calc(100dvh - 292px),760px)", css)
        self.assertIn("align-items:stretch", css)
        self.assertIn(".lora-admin-list-pane .manager-list{display:block;height:100%;max-height:100%", css)
        self.assertIn("grid-template-columns:minmax(260px,.78fr) minmax(0,1.22fr)", css)
        self.assertIn("overflow-x:hidden", css)
        self.assertIn(".lora-meta-editor-shell .tip-meta-grid{grid-template-columns:repeat(2,minmax(0,1fr))}", css)
        self.assertIn(".lora-meta-editor-shell.mobile-open", css)
        self.assertIn("align-content:start;grid-auto-rows:max-content", css)
        self.assertIn("box-shadow:inset 3px 0 0 var(--blue)", css)
        self.assertIn("async function renameLoraMetadata()", script)
        self.assertIn("请先保存当前说明，再修改文件名。", script)
        self.assertIn(".canvas-ui #view-tips .admin-help-tabs", interface)
        self.assertIn("grid-template-columns: repeat(2,minmax(108px,1fr))", interface)
        self.assertIn(".canvas-ui #view-tips .admin-help-workspace", interface)
        self.assertIn(".canvas-ui #view-tips .page-help-editor-shell.mobile-open", interface)
        self.assertIn("box-shadow: inset 3px 0 0 #9fc8ff", interface)
        self.assertIn("html,body{max-width:100%;overflow-x:hidden}", css)
        self.assertIn(".lora-row>*", css)
        self.assertIn(".lora-row select,.lora-row input{width:100%;min-width:0;max-width:100%}", css)
        self.assertIn("grid-template-columns:minmax(280px,.92fr) minmax(0,1.08fr)", css)
        self.assertIn(".dialog-panel>header{display:flex;align-items:flex-start;justify-content:space-between", css)
        self.assertIn(".page-help-dialog>header,.lora-guide-panel>header{margin-bottom:16px}", css)
        self.assertIn(".dialog-actions.vertical{display:grid;grid-template-columns:1fr}", css)
        self.assertIn("dialog:not(.media-viewer):not(.detail-lightbox):not(.image-editor-dialog)", css)
        self.assertIn("#lora-guide-dialog{width:min(860px,calc(100vw - 24px))", css)
        self.assertIn("grid-template-rows:auto auto minmax(0,1fr) auto", css)
        self.assertIn("#lora-guide-dialog .lora-guide-list{min-width:0;min-height:0;max-width:100%", css)
        self.assertIn("#lora-guide-dialog .lora-guide-detail-open{position:absolute", css)
        self.assertIn(".lora-guide-detail-body p{margin:22px 0 0", css)
        self.assertIn("--lora-safe-block: max(12px,env(safe-area-inset-top,0px),env(safe-area-inset-bottom,0px))", interface)
        self.assertIn("border-radius: inherit", interface)
        self.assertIn("state.loraGuideDetailTrigger=null", script)
        self.assertIn("detail.setAttribute('aria-hidden','false')", script)
        self.assertIn("overflow-wrap:anywhere;word-break:break-word", css)
        self.assertNotIn("preprocessJob", script)
        self.assertIn("scene_ref_boost", script)
        self.assertNotIn("tipsFor(", script)
        self.assertNotIn("tipsDisclosure(", script)
        self.assertNotIn("restoreSavedPreprocess", script)
        self.assertNotIn("discardPreprocess", script)
        self.assertIn("identity?'源图与图生图要求':'图片与编辑要求'", script)
        self.assertIn("const familyNote=$('#lora-family');if(familyNote)", script)
        self.assertIn("input.append(new Option('原图编辑（保留未指定区域）'", script)
        self.assertIn("input.append(new Option('沿用原图比例（按像素预算）'", script)
        self.assertIn("input.append(new Option('Fit（推荐，完整适配原图）'", script)
        self.assertIn("root.hidden=!expectsImage", script)
        self.assertIn('class="desktop-upload-hint"', html)
        self.assertIn('id="image-upload-name"', html)
        self.assertIn('id="flux-upload-name"', html)
        self.assertEqual(html.count('class="upload-file-input"'), 3)
        self.assertIn("function updateUploadName(target,fileName='',fallback='未选择图片')", script)
        self.assertIn("function maxGenerationCount()", script)
        self.assertIn("state.user?.max_generation_count", script)
        self.assertIn("repeat(auto-fit,minmax(300px,520px))", css)
        self.assertNotIn(".detail-lora-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))", css)
        self.assertIn('id="image-clear"', html)
        self.assertIn('id="flux-image-clear"', html)
        self.assertIn("document.addEventListener('paste'", script)
        self.assertIn("document.addEventListener('drop'", script)
        self.assertIn("event.key!=='Enter'", script)
        self.assertIn("target.searchParams.delete('rerun')", script)
        self.assertIn("function normalizedImageFile(file)", script)
        self.assertIn("const PENDING_CREATE_RESET_KEY='mobilePendingCreateReset'", script)
        self.assertIn("function createDraftSignature()", script)
        self.assertIn("function finalizePendingCreateReset(job=null)", script)
        self.assertIn("job.status==='completed'&&createDraftSignature()===pending.signature", script)
        self.assertNotIn("submittedWorkflow", script)
        self.assertIn("await finalizePendingCreateReset(job);await loadActivity()", script)
        self.assertIn('id="media-viewer"', html)
        self.assertIn("function openMediaViewer(src", script)
        self.assertIn("#view-detail .detail-images{grid-template-columns:minmax(0,1fr)", css)
        self.assertIn("height:min(72vh,800px)", css)
        self.assertIn('id="reuse-dialog"', html)
        self.assertIn("function reusableResultRef(src,item,label)", script)
        self.assertIn("function loadReusedResult()", script)
        self.assertIn("appendReuseFields(data)", script)
        self.assertIn(".result-reuse", css)
        self.assertNotIn("unknown_prompt_style", script)
        self.assertNotIn("/api/prompt-tools", script)
        interrogate_start = script.index("function interrogateResultButton")
        interrogate_end = script.index("function installComparisonActivity", interrogate_start)
        self.assertNotIn("targetWorkflow!=='krea-identity-edit'", script[interrogate_start:interrogate_end])
        self.assertIn("quick.textContent='查看原图'", script)
        self.assertNotIn("if(job.workflow==='removed-workflow'){quick.textContent='反推后再生成'", script)
        self.assertNotIn('id="reuse-interrogate"', html)
        self.assertIn("if(interrogate)card.append(interrogate);card.append(deleteImageButton(item))", script)
        self.assertIn("身份重塑（姿势/场景可重建）", script)
        self.assertIn("源图 / 身份参考图", script)
        self.assertIn(".result-interrogate{position:absolute;z-index:6;right:82px;bottom:46px", css)
        self.assertNotIn('id="prompt-assistant-dialog"', html)
        self.assertIn("@media(min-width:1100px)", css)
        self.assertIn("grid-template-columns:minmax(0,2fr)", css)
        self.assertIn("function historyLoraChip(item)", script)
        self.assertIn("className='lora-weight'", script)
        self.assertIn(".history-params .lora-chip .lora-weight", css)
        self.assertIn("function queueRunningDetail(task)", script)
        self.assertIn("task.progress_exact", script)
        self.assertIn("预计等待时间暂不稳定", script)
        self.assertNotIn("function queueProgressText(task){return`${task.progress_approximate", script)
        self.assertNotIn("预计剩余", script)


if __name__ == "__main__":
    unittest.main()
