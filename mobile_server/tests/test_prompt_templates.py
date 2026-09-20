from __future__ import annotations

import asyncio
import gc
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

import mobile_server.app as app_module


class PromptTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_database = app_module.DATABASE
        app_module.DATABASE = Path(self.temporary.name) / "templates.sqlite3"
        app_module.init_database()

    def tearDown(self) -> None:
        app_module.DATABASE = self.original_database
        gc.collect()
        self.temporary.cleanup()

    def test_default_templates_are_seeded_once_and_are_deletable(self) -> None:
        templates = app_module.all_prompt_templates(app_module.PROMPT_TEMPLATE_SCOPE)
        self.assertEqual(len(templates), 2)
        self.assertEqual(
            [item["name"] for item in templates],
            ["局部替换", "表情调整"],
        )
        self.assertTrue(all("[" not in item["text"].replace("[]", "") for item in templates))

        asyncio.run(app_module.delete_prompt_template(templates[0]["id"]))
        app_module.init_database()
        remaining = app_module.all_prompt_templates(app_module.PROMPT_TEMPLATE_SCOPE)
        self.assertEqual(len(remaining), 1)
        self.assertNotIn("局部替换", {item["name"] for item in remaining})

    def test_v1_shared_templates_migrate_without_removing_private_templates(self) -> None:
        with app_module.sqlite3.connect(app_module.DATABASE) as db:
            db.execute("DELETE FROM prompt_templates")
            db.execute(
                "INSERT INTO prompt_templates(name,body,scope,sort_order,created_at,updated_at,owner_id,is_system) VALUES(?,?,?,?,?,?,NULL,1)",
                ("旧共享模板", "旧内容", app_module.PROMPT_TEMPLATE_SCOPE, 0, app_module.utc_now(), app_module.utc_now()),
            )
            db.execute(
                "INSERT INTO prompt_templates(name,body,scope,sort_order,created_at,updated_at,owner_id,is_system) VALUES(?,?,?,?,?,?,?,0)",
                ("私人模板", "私人内容", app_module.PROMPT_TEMPLATE_SCOPE, 9, app_module.utc_now(), app_module.utc_now(), 123),
            )
            db.execute("UPDATE app_metadata SET value='1' WHERE key='prompt_template_seed_version'")

        app_module.init_database()
        with app_module.sqlite3.connect(app_module.DATABASE) as db:
            db.row_factory = app_module.sqlite3.Row
            rows = [dict(row) for row in db.execute("SELECT name,is_system,owner_id FROM prompt_templates ORDER BY is_system DESC,sort_order")]
            version = db.execute("SELECT value FROM app_metadata WHERE key='prompt_template_seed_version'").fetchone()[0]
        self.assertEqual(version, "2")
        self.assertEqual([row["name"] for row in rows if row["is_system"]], [item[0] for item in app_module.DEFAULT_PROMPT_TEMPLATES])
        self.assertEqual([row for row in rows if not row["is_system"]], [{"name": "私人模板", "is_system": 0, "owner_id": 123}])

    def test_template_crud_and_validation(self) -> None:
        created = asyncio.run(app_module.create_prompt_template({
            "name": "我的模板", "text": "仅编辑指定区域。", "scope": app_module.PROMPT_TEMPLATE_SCOPE,
        }))
        self.assertEqual(created["name"], "我的模板")
        self.assertEqual(created["text"], "仅编辑指定区域。")

        updated = asyncio.run(app_module.update_prompt_template(created["id"], {
            "name": "改名模板", "text": "修改后的模板正文。",
        }))
        self.assertEqual(updated["name"], "改名模板")
        self.assertEqual(updated["text"], "修改后的模板正文。")

        with self.assertRaises(HTTPException) as duplicate:
            asyncio.run(app_module.create_prompt_template({
                "name": "改名模板", "text": "重复名称", "scope": app_module.PROMPT_TEMPLATE_SCOPE,
            }))
        self.assertEqual(duplicate.exception.status_code, 409)

        with self.assertRaises(HTTPException) as empty:
            asyncio.run(app_module.create_prompt_template({
                "name": "", "text": "正文", "scope": app_module.PROMPT_TEMPLATE_SCOPE,
            }))
        self.assertEqual(empty.exception.status_code, 400)

        asyncio.run(app_module.delete_prompt_template(created["id"]))
        with self.assertRaises(HTTPException) as missing:
            app_module.prompt_template(created["id"])
        self.assertEqual(missing.exception.status_code, 404)

    def test_invalid_scope_and_long_text_are_rejected(self) -> None:
        with self.assertRaises(HTTPException) as scope:
            app_module.all_prompt_templates("krea")
        self.assertEqual(scope.exception.status_code, 400)

        with self.assertRaises(HTTPException) as body:
            asyncio.run(app_module.create_prompt_template({
                "name": "过长模板", "text": "x" * 6001, "scope": app_module.PROMPT_TEMPLATE_SCOPE,
            }))
        self.assertEqual(body.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
