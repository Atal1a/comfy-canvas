from __future__ import annotations

import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi import UploadFile
from io import BytesIO

import mobile_server.app as app_module


class HistoryRerunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.original_database = app_module.DATABASE
        self.original_comfy_root = app_module.COMFY_ROOT
        self.original_upload_dir = app_module.UPLOAD_DIR
        app_module.DATABASE = root / "jobs.sqlite3"
        app_module.COMFY_ROOT = root / "ComfyUI"
        app_module.UPLOAD_DIR = app_module.COMFY_ROOT / "input" / "mobile_uploads"
        app_module.UPLOAD_DIR.mkdir(parents=True)
        app_module.init_database()

    def tearDown(self) -> None:
        app_module.DATABASE = self.original_database
        app_module.COMFY_ROOT = self.original_comfy_root
        app_module.UPLOAD_DIR = self.original_upload_dir
        gc.collect()
        self.temporary.cleanup()

    def insert_group(self, group_id: str, source: dict[str, str], storage_scope: str = "mobile") -> None:
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,source_image_json,storage_scope) "
                "VALUES(?,?,?,?,?,?,?)",
                (group_id, "qwen2511-modular-flux2", "test prompt", "completed", app_module.utc_now(), json.dumps(source), storage_scope),
            )

    def test_source_image_is_copied_for_an_independent_rerun(self) -> None:
        original = app_module.UPLOAD_DIR / "original.png"
        original.write_bytes(b"historical-image")
        self.insert_group("group-old", {"filename": original.name, "subfolder": "mobile_uploads", "type": "input"})

        image_name, source = app_module.copy_group_source_image("group-old")
        copied = app_module.UPLOAD_DIR / source["filename"]

        self.assertEqual(image_name, f"mobile_uploads/{copied.name}")
        self.assertNotEqual(copied, original)
        self.assertEqual(copied.read_bytes(), b"historical-image")

        app_module.remove_source_image(app_module.db_group("group-old") or {})
        self.assertFalse(original.exists())
        self.assertTrue(copied.exists())

    def test_face_reference_is_copied_and_cleaned_with_its_task(self) -> None:
        source = app_module.UPLOAD_DIR / "scene.png"
        reference = app_module.UPLOAD_DIR / "person.png"
        source.write_bytes(b"scene")
        reference.write_bytes(b"person")
        source_record = {
            "filename": source.name, "subfolder": "mobile_uploads", "type": "input",
        }
        reference_record = {
            "filename": reference.name, "subfolder": "mobile_uploads", "type": "input",
        }
        self.insert_group("group-face", source_record)
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "UPDATE generation_groups SET reference_image_json=? WHERE group_id=?",
                (json.dumps(reference_record), "group-face"),
            )

        _, copied = app_module.copy_group_reference_image("group-face")
        copied_path = app_module.safe_source_path(copied)
        self.assertTrue(copied_path.is_file())
        self.assertEqual(copied_path.read_bytes(), b"person")

        app_module.remove_source_image(app_module.db_group("group-face") or {})
        self.assertFalse(source.exists())
        self.assertFalse(reference.exists())
        self.assertTrue(copied_path.exists())

    def test_missing_or_non_mobile_history_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as missing:
            app_module.copy_group_source_image("missing")
        self.assertEqual(missing.exception.status_code, 404)

        original = app_module.UPLOAD_DIR / "legacy.png"
        original.write_bytes(b"legacy-image")
        self.insert_group(
            "group-legacy",
            {"filename": original.name, "subfolder": "mobile_uploads", "type": "input"},
            storage_scope="legacy",
        )
        with self.assertRaises(HTTPException) as legacy:
            app_module.copy_group_source_image("group-legacy")
        self.assertEqual(legacy.exception.status_code, 404)

    def test_missing_historical_file_requests_a_new_upload(self) -> None:
        self.insert_group("group-missing-file", {"filename": "gone.webp", "subfolder": "mobile_uploads", "type": "input"})
        with self.assertRaises(HTTPException) as missing:
            app_module.copy_group_source_image("group-missing-file")
        self.assertEqual(missing.exception.status_code, 409)

    def test_empty_browser_file_part_falls_back_to_historical_source(self) -> None:
        original = app_module.UPLOAD_DIR / "original.jpg"
        original.write_bytes(b"historical-image")
        self.insert_group("group-old", {"filename": original.name, "subfolder": "mobile_uploads", "type": "input"})
        empty_upload = UploadFile(filename="", file=BytesIO())
        spec = app_module.get_spec("qwen2511-modular-flux2")
        settings = json.dumps(app_module.workflow_descriptor(spec)["defaults"])

        with patch.object(app_module, "build_graph", return_value=({}, {})), patch.object(
            app_module, "comfy_json", new=AsyncMock(return_value={"prompt_id": "rerun-prompt"})
        ):
            result = __import__("asyncio").run(
                app_module.submit_job("qwen2511-modular-flux2", "rerun prompt", settings, empty_upload, "group-old")
            )

        self.assertNotEqual(result["id"], "group-old")
        copied = app_module.source_image_from(app_module.db_group(result["id"]) or {})
        self.assertIsNotNone(copied)
        self.assertNotEqual(copied["filename"], original.name)
        self.assertEqual((app_module.UPLOAD_DIR / copied["filename"]).read_bytes(), b"historical-image")


if __name__ == "__main__":
    unittest.main()
