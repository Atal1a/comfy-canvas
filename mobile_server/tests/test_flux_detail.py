from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import mobile_server.app as app_module



class FluxDetailLifecycleTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(app_module, name, value)
        gc.collect()
        self.temporary.cleanup()

    def insert_group(self, workflow: str = "qwen2511-modular-flux2") -> tuple[str, str, dict[str, str]]:
        group_id, item_id = "group-detail", "item-one"
        output = {"filename": "edit.png", "subfolder": "mobile/qwen2511-modular-flux2", "type": "output"}
        path = app_module.OUTPUT_DIR / output["subfolder"] / output["filename"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"edit")
        items = [{"id": item_id, "prompt_id": "edit-prompt", "final": output, "stage1": None}]
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,items_json,storage_scope) VALUES(?,?,?,?,?,?,'mobile')",
                (group_id, workflow, "edit", "completed", app_module.utc_now(), json.dumps(items)),
            )
        return group_id, item_id, output

    def test_successful_detail_replaces_old_detail_and_stale_upscale(self) -> None:
        group_id, item_id, _ = self.insert_group()
        old_detail = {"filename": "old-detail.png", "subfolder": "mobile/flux-detail", "type": "output"}
        old_upscale = {"filename": "old-upscale.png", "subfolder": "mobile/upscale", "type": "output"}
        new_detail = {"filename": "new-detail.png", "subfolder": "mobile/flux-detail", "type": "output"}
        for image in (old_detail, old_upscale, new_detail):
            path = app_module.OUTPUT_DIR / image["subfolder"] / image["filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"result")
        source = {"filename": "detail-source.png", "subfolder": "mobile_uploads", "type": "input"}
        (app_module.UPLOAD_DIR / source["filename"]).write_bytes(b"source")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO detail_jobs(detail_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,output_json) VALUES(?,?,?,?,?,?,?)",
                ("old", "old-prompt", group_id, item_id, "completed", "2026-01-01", json.dumps(old_detail)),
            )
            db.execute(
                "INSERT INTO detail_jobs(detail_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,source_image_json) VALUES(?,?,?,?,?,?,?)",
                ("new", "new-prompt", group_id, item_id, "running", "2026-01-02", json.dumps(source)),
            )
            db.execute(
                "INSERT INTO upscale_jobs(upscale_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,output_json) VALUES(?,?,?,?,?,?,?)",
                ("up", "up-prompt", group_id, item_id, "completed", "2026-01-01", json.dumps(old_upscale)),
            )
        history = {"new-prompt": {"status": {"status_str": "success"}, "outputs": {"425": {"images": [new_detail]}}}}
        with patch.object(app_module, "comfy_json", new=AsyncMock(return_value=history)):
            result = asyncio.run(app_module.refresh_detail("new"))
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(app_module.db_detail("old"))
        self.assertIsNone(app_module.db_upscale("up"))
        self.assertTrue((app_module.OUTPUT_DIR / new_detail["subfolder"] / new_detail["filename"]).exists())

    def test_upscale_ignores_completed_legacy_detail(self) -> None:
        group_id, item_id, edit = self.insert_group()
        detail = {"filename": "detail.png", "subfolder": "mobile/flux-detail", "type": "output"}
        detail_path = app_module.OUTPUT_DIR / detail["subfolder"] / detail["filename"]
        detail_path.parent.mkdir(parents=True)
        detail_path.write_bytes(b"flux-detail")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO detail_jobs(detail_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,output_json) VALUES(?,?,?,?,?,?,?)",
                ("detail", "detail-prompt", group_id, item_id, "completed", "2026-01-01", json.dumps(detail)),
            )
        with patch.object(app_module, "build_enhanced_upscale_graph", return_value=({}, {})), patch.object(
            app_module, "comfy_json", new=AsyncMock(return_value={"prompt_id": "upscale-prompt"}),
        ):
            result = asyncio.run(app_module.submit_upscale(group_id, item_id, {"engine": "flux2", "resolution": 2048}))
        self.assertEqual(result["source_kind"], "edit")
        row = app_module.db_upscale(result["id"])
        copied = json.loads(row["source_image_json"])
        self.assertEqual((app_module.UPLOAD_DIR / copied["filename"]).read_bytes(), (app_module.OUTPUT_DIR / edit["subfolder"] / edit["filename"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
