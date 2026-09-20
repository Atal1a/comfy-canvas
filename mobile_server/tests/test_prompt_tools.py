from __future__ import annotations

import asyncio
import gc
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.datastructures import Headers

import mobile_server.app as app_module


class PromptToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        names = (
            "DATABASE", "COMFY_ROOT", "UPLOAD_DIR", "OUTPUT_DIR", "MOBILE_OUTPUT_DIR",
            "LLAMA_ENHANCE_WORKFLOW", "LLAMA_IMAGE_PROMPT_WORKFLOW",
        )
        self.originals = {name: getattr(app_module, name) for name in names}
        app_module.DATABASE = root / "prompt-tools.sqlite3"
        app_module.COMFY_ROOT = root / "ComfyUI"
        app_module.UPLOAD_DIR = app_module.COMFY_ROOT / "input" / "mobile_uploads"
        app_module.OUTPUT_DIR = root / "output"
        app_module.MOBILE_OUTPUT_DIR = app_module.OUTPUT_DIR / "mobile"
        app_module.UPLOAD_DIR.mkdir(parents=True)
        app_module.MOBILE_OUTPUT_DIR.mkdir(parents=True)
        model_root = app_module.COMFY_ROOT / "models" / "LLM"
        model_root.mkdir(parents=True)
        (model_root / app_module.LLAMA_MODEL).touch()
        (model_root / app_module.LLAMA_MMPROJ).touch()
        app_module.LLAMA_ENHANCE_WORKFLOW = self.originals["LLAMA_ENHANCE_WORKFLOW"]
        app_module.LLAMA_IMAGE_PROMPT_WORKFLOW = self.originals["LLAMA_IMAGE_PROMPT_WORKFLOW"]
        app_module.SCHEDULER_LOCK = None
        app_module.init_database()
        self.alice = app_module.create_user_account("alice", "alice-secure-password")
        self.bob = app_module.create_user_account("bob", "bob-secure-password")

    def tearDown(self) -> None:
        app_module.CURRENT_USER.set(None)
        app_module.SCHEDULER_LOCK = None
        for name, value in self.originals.items():
            setattr(app_module, name, value)
        gc.collect()
        self.temporary.cleanup()

    def image_upload(self) -> UploadFile:
        buffer = BytesIO()
        Image.new("RGB", (32, 24), "white").save(buffer, "PNG")
        buffer.seek(0)
        return UploadFile(filename="source.png", file=buffer, headers=Headers({"content-type": "image/png"}))

    def test_retired_graph_builder_rejects_execution(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            app_module.build_prompt_tool_graph({})
        self.assertEqual(caught.exception.status_code, 410)

    def test_retired_submission_never_enqueues(self) -> None:
        with patch.object(app_module, "scheduler_tick", AsyncMock()) as scheduler:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(app_module.submit_prompt_tool(operation="interrogate", target_workflow="krea-identity-edit", image=self.image_upload()))
            self.assertEqual(caught.exception.status_code, 410)
            scheduler.assert_not_awaited()

    def test_completed_output_is_saved_and_temp_image_removed(self) -> None:
        source = {"filename": "temp.png", "subfolder": "mobile_uploads", "type": "input"}
        (app_module.UPLOAD_DIR / "temp.png").write_bytes(b"temporary")
        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO prompt_tool_jobs(tool_id,owner_id,operation,target_workflow,mode,style,input_text,status,submitted_at,prompt_id,source_image_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("tool-one", self.alice["user_id"], "interrogate", "qwen2511-modular-flux2", "pure", "detailed", "", "running", now, "comfy-one", json.dumps(source)),
            )
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            comfy = AsyncMock(return_value={"comfy-one": {"status": {"status_str": "success"}, "outputs": {"21": {"text": ["```\n中文提示词\n```"]}}}})
            with patch.object(app_module, "comfy_json", comfy):
                result = asyncio.run(app_module.refresh_prompt_tool("tool-one"))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["output"], "中文提示词")
            self.assertFalse((app_module.UPLOAD_DIR / "temp.png").exists())
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_enhance_requires_input(self) -> None:
        with self.assertRaises(HTTPException):
            app_module.normalize_prompt_tool_values({"operation": "enhance", "input": ""})

    def test_reasoning_draft_is_removed_from_prompt_output(self) -> None:
        raw = "第一段推理草稿\n</think>\n\n```text\n最终提示词正文\n```"
        self.assertEqual(app_module.clean_prompt_tool_output(raw), "最终提示词正文")
        row = {
            "tool_id": "completed", "operation": "interrogate", "target_workflow": "krea-identity-edit",
            "mode": "pure", "style": "detailed", "input_text": "", "status": "completed",
            "submitted_at": app_module.utc_now(), "completed_at": app_module.utc_now(),
            "output_text": raw, "error_message": None, "source_image_json": None,
        }
        self.assertEqual(app_module.public_prompt_tool(row)["output"], "最终提示词正文")

    def test_unknown_prompt_style_is_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            app_module.normalize_prompt_tool_values({
                "operation": "enhance", "style": "unknown", "input": "原提示词",
            })


if __name__ == "__main__":
    unittest.main()
