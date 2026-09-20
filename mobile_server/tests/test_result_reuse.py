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

import mobile_server.app as app_module


class ResultReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.originals = {name: getattr(app_module, name) for name in (
            "DATABASE", "COMFY_ROOT", "UPLOAD_DIR", "OUTPUT_DIR", "MOBILE_OUTPUT_DIR",
        )}
        app_module.DATABASE = root / "jobs.sqlite3"
        app_module.COMFY_ROOT = root / "ComfyUI"
        app_module.UPLOAD_DIR = app_module.COMFY_ROOT / "input" / "mobile_uploads"
        app_module.OUTPUT_DIR = root / "output"
        app_module.MOBILE_OUTPUT_DIR = app_module.OUTPUT_DIR / "mobile"
        app_module.UPLOAD_DIR.mkdir(parents=True)
        app_module.MOBILE_OUTPUT_DIR.mkdir(parents=True)
        app_module.init_database()
        self.group_id = "group-result"
        self.item_id = "item-result"
        self.edit = self.make_output("qwen/edit.png", b"edited")
        items = [{"id": self.item_id, "prompt_id": "prompt", "final": self.edit, "stage1": None}]
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,parameters_json,items_json,storage_scope) VALUES(?,?,?,?,?,?,?,'mobile')",
                (self.group_id, "qwen2511-modular-flux2", "old prompt", "completed", app_module.utc_now(), "{}", json.dumps(items)),
            )

    def tearDown(self) -> None:
        app_module.CURRENT_USER.set(None)
        for name, value in self.originals.items():
            setattr(app_module, name, value)
        gc.collect()
        self.temporary.cleanup()

    def make_output(self, relative: str, body: bytes) -> dict[str, str]:
        path = app_module.MOBILE_OUTPUT_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {"filename": path.name, "subfolder": str(path.parent.relative_to(app_module.OUTPUT_DIR)), "type": "output"}

    def test_edit_and_upscale_results_are_resolved(self) -> None:
        self.assertEqual(app_module.reusable_result_image(self.group_id, self.item_id, "edit"), self.edit)
        upscale = self.make_output("upscale/result.webp", b"upscaled")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO upscale_jobs(upscale_id,parent_group_id,parent_item_id,status,submitted_at,output_json) VALUES(?,?,?,?,?,?)",
                ("upscale-result", self.group_id, self.item_id, "completed", app_module.utc_now(), json.dumps(upscale)),
            )
        self.assertEqual(app_module.reusable_result_image(self.group_id, self.item_id, "upscale", "upscale-result"), upscale)

    def test_incomplete_or_missing_result_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as missing:
            app_module.reusable_result_image(self.group_id, self.item_id, "upscale", "missing")
        self.assertEqual(missing.exception.status_code, 404)
        with self.assertRaises(HTTPException) as invalid:
            app_module.reusable_result_image(self.group_id, self.item_id, "preprocess")
        self.assertEqual(invalid.exception.status_code, 400)

    def test_other_users_cannot_reuse_private_results(self) -> None:
        owner = app_module.create_user_account("result-owner", "owner-password-123")
        visitor = app_module.create_user_account("result-visitor", "visitor-password-123")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute("UPDATE generation_groups SET owner_id=? WHERE group_id=?", (owner["user_id"], self.group_id))
        token = app_module.CURRENT_USER.set(visitor)
        try:
            with self.assertRaises(HTTPException) as hidden:
                app_module.reusable_result_image(self.group_id, self.item_id, "edit")
            self.assertEqual(hidden.exception.status_code, 404)
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_submit_copies_selected_result_independently(self) -> None:
        spec = app_module.get_spec("qwen2511-modular-flux2")
        settings = json.dumps(app_module.workflow_descriptor(spec)["defaults"])
        with patch.object(app_module, "build_graph", return_value=({}, {})), patch.object(
            app_module, "comfy_json", new=AsyncMock(return_value={"prompt_id": "continued-prompt"}),
        ):
            result = asyncio.run(app_module.submit_job(
                workflow=spec.key, prompt="next edit", settings=settings, image=None,
                source_group_id=None, preprocess_id=None, result_group_id=self.group_id,
                result_item_id=self.item_id, result_source_kind="edit", result_record_id=None,
            ))
        copied = app_module.source_image_from(app_module.db_group(result["id"]) or {})
        self.assertIsNotNone(copied)
        copied_path = app_module.safe_source_path(copied)
        self.assertTrue(copied_path.is_file())
        self.assertEqual(copied_path.read_bytes(), b"edited")
        original = app_module.safe_output_path(self.edit)
        original.unlink()
        self.assertTrue(copied_path.is_file())

if __name__ == "__main__":
    unittest.main()
