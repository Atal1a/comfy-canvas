from __future__ import annotations

import asyncio
import gc
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import mobile_server.app as app_module


class PreEditUpscaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.originals = {
            name: getattr(app_module, name)
            for name in ("DATABASE", "COMFY_ROOT", "UPLOAD_DIR", "OUTPUT_DIR", "MOBILE_OUTPUT_DIR")
        }
        app_module.DATABASE = root / "jobs.sqlite3"
        app_module.COMFY_ROOT = root / "ComfyUI"
        app_module.UPLOAD_DIR = app_module.COMFY_ROOT / "input" / "mobile_uploads"
        app_module.OUTPUT_DIR = root / "output"
        app_module.MOBILE_OUTPUT_DIR = app_module.OUTPUT_DIR / "mobile"
        app_module.UPLOAD_DIR.mkdir(parents=True)
        app_module.MOBILE_OUTPUT_DIR.mkdir(parents=True)
        app_module.init_database()

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(app_module, name, value)
        gc.collect()
        self.temporary.cleanup()

    def test_edit_result_color_is_matched_to_the_original_upload(self) -> None:
        graph = {"save": {"class_type": "SaveImage", "inputs": {"images": ["edit", 0]}}}
        app_module.apply_pre_edit_color_match(graph, "mobile_uploads/original.png")
        self.assertEqual(graph["pre_original_0"]["inputs"]["image"], "mobile_uploads/original.png")
        self.assertEqual(graph["pre_color_match_0"]["inputs"]["image_ref"], ["pre_original_0", 0])
        self.assertEqual(graph["pre_color_match_0"]["inputs"]["image_target"], ["edit", 0])
        self.assertEqual(graph["pre_color_match_0"]["inputs"]["strength"], 1.0)
        self.assertEqual(graph["save"]["inputs"]["images"], ["pre_color_match_0", 0])

    def test_preprocessed_generation_parameter_is_rejected(self) -> None:
        spec = app_module.get_spec("qwen2511-modular-flux2")
        settings = json.dumps(app_module.workflow_descriptor(spec)["defaults"])
        with self.assertRaises(HTTPException) as legacy_submit:
            asyncio.run(app_module.submit_job(
                "qwen2511-modular-flux2", "edit the image", settings,
                preprocess_id="pre-existing",
            ))
        self.assertEqual(legacy_submit.exception.status_code, 410)

    def test_expired_preprocess_files_are_removed_but_recent_files_remain(self) -> None:
        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        records = []
        for name, age_minutes in (("expired", 70), ("recent", 30)):
            source = {"filename": f"{name}.png", "subfolder": "mobile_uploads", "type": "input"}
            output = {"filename": f"{name}.png", "subfolder": "mobile/preprocess", "type": "output"}
            source_path = app_module.UPLOAD_DIR / source["filename"]
            output_path = app_module.OUTPUT_DIR / output["subfolder"] / output["filename"]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(name.encode())
            output_path.write_bytes(name.encode())
            completed = (now - timedelta(minutes=age_minutes)).isoformat()
            with sqlite3.connect(app_module.DATABASE) as db:
                db.execute(
                    "INSERT INTO preprocess_jobs(preprocess_id,prompt_id,status,submitted_at,completed_at,output_json,parameters_json,source_image_json) VALUES(?,?,?,?,?,?,?,?)",
                    (name, f"prompt-{name}", "completed", completed, completed, json.dumps(output), "{}", json.dumps(source)),
                )
            records.append((name, source_path, output_path))

        removed = asyncio.run(app_module.cleanup_expired_preprocesses(now))
        self.assertEqual(removed, 1)
        self.assertIsNone(app_module.db_preprocess("expired"))
        self.assertFalse(records[0][1].exists())
        self.assertFalse(records[0][2].exists())
        self.assertIsNotNone(app_module.db_preprocess("recent"))
        self.assertTrue(records[1][1].is_file())
        self.assertTrue(records[1][2].is_file())

    def test_stale_active_preprocess_is_cancelled_before_cleanup(self) -> None:
        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        submitted = (now - timedelta(minutes=70)).isoformat()
        source = {"filename": "active.png", "subfolder": "mobile_uploads", "type": "input"}
        source_path = app_module.UPLOAD_DIR / source["filename"]
        source_path.write_bytes(b"active")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO preprocess_jobs(preprocess_id,prompt_id,status,submitted_at,parameters_json,source_image_json) VALUES(?,?,?,?,?,?)",
                ("active", "active-prompt", "running", submitted, "{}", json.dumps(source)),
            )

        async def mark_cancelled(kind: str, record_id: str):
            self.assertEqual((kind, record_id), ("preprocess", "active"))
            with sqlite3.connect(app_module.DATABASE) as db:
                db.execute(
                    "UPDATE preprocess_jobs SET status='cancelled',completed_at=? WHERE preprocess_id=?",
                    (now.isoformat(), record_id),
                )
            return {}

        with patch.object(app_module, "refresh_preprocess", new=AsyncMock(return_value={})), patch.object(
            app_module, "cancel_postprocess", new=AsyncMock(side_effect=mark_cancelled),
        ) as cancel:
            removed = asyncio.run(app_module.cleanup_expired_preprocesses(now))
        self.assertEqual(removed, 1)
        cancel.assert_awaited_once_with("preprocess", "active")
        self.assertIsNone(app_module.db_preprocess("active"))
        self.assertFalse(source_path.exists())


if __name__ == "__main__":
    unittest.main()
