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
from mobile_server.workflow_compiler import ensure_enhanced_upscale_workflow


class EnhancedUpscaleGraphTests(unittest.TestCase):
    def test_reference_workflow_compiles_with_explicit_model_connections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / "enhanced.json"
            ensure_enhanced_upscale_workflow(app_module.ENHANCED_UPSCALE_WORKFLOW, workflow)
            original_workflow, original_comfy = app_module.ENHANCED_UPSCALE_WORKFLOW, app_module.COMFY_ROOT
            app_module.ENHANCED_UPSCALE_WORKFLOW = workflow
            app_module.COMFY_ROOT = root / "ComfyUI"
            required = (
                app_module.COMFY_ROOT / "models" / "diffusion_models" / Path(app_module.ENHANCED_UPSCALE_MODEL),
                app_module.COMFY_ROOT / "models" / "text_encoders" / Path(app_module.ENHANCED_UPSCALE_TEXT_ENCODER),
                app_module.COMFY_ROOT / "models" / "vae" / Path(app_module.ENHANCED_UPSCALE_VAE),
                app_module.COMFY_ROOT / "models" / "SEEDVR2" / Path(app_module.ENHANCED_UPSCALE_SEEDVR_DIT),
                app_module.COMFY_ROOT / "models" / "SEEDVR2" / Path(app_module.ENHANCED_UPSCALE_SEEDVR_VAE),
                app_module.COMFY_ROOT / "models" / "loras" / Path(app_module.ENHANCED_UPSCALE_LORA),
            )
            for model in required:
                model.parent.mkdir(parents=True, exist_ok=True)
                model.touch()
            try:
                settings = app_module.normalize_enhanced_upscale_settings({
                    "resolution": 4096, "strength": 0.65, "grain": 0.15,
                    "random_seed": False, "seed": 11,
                })
                graph, _ = app_module.build_enhanced_upscale_graph(settings, "mobile_uploads/test.png")
            finally:
                app_module.ENHANCED_UPSCALE_WORKFLOW = original_workflow
                app_module.COMFY_ROOT = original_comfy
            self.assertEqual(graph["186"]["inputs"]["scale_to_length"], 4096)
            self.assertEqual(graph["223"]["inputs"]["lora_name"], app_module.ENHANCED_UPSCALE_LORA)
            self.assertEqual(graph["223"]["inputs"]["strength_model"], 0.65)
            self.assertEqual(graph["201"]["inputs"]["clip"], ["5", 0])
            self.assertEqual(graph["201"]["inputs"]["vae"], ["6", 0])
            self.assertEqual(graph["201"]["inputs"]["image1"], ["9001", 0])
            self.assertEqual(graph["9001"]["class_type"], "VRAMCleanup")
            self.assertEqual(graph["9001"]["inputs"]["anything"], ["202", 0])
            self.assertTrue(graph["9001"]["inputs"]["offload_model"])
            self.assertEqual(graph["211"]["inputs"]["model"], ["223", 0])
            self.assertEqual(graph["211"]["inputs"]["sampler_name"], "res_2s")
            self.assertEqual(graph["139"]["inputs"]["dit"], ["132", 0])

    def test_only_named_enhanced_quality_sizes_are_allowed(self) -> None:
        for resolution in (2048, 3072, 4096):
            self.assertEqual(app_module.normalize_enhanced_upscale_settings({"resolution": resolution})["resolution"], resolution)
        with self.assertRaises(HTTPException):
            app_module.normalize_enhanced_upscale_settings({"resolution": 2560})

    def test_style_profiles_supply_safe_defaults_and_keep_legacy_default(self) -> None:
        detail = app_module.normalize_enhanced_upscale_settings({"random_seed": False, "seed": 1})
        soft = app_module.normalize_enhanced_upscale_settings({
            "style_profile": "realistic_soft", "random_seed": False, "seed": 1,
        })
        anime = app_module.normalize_enhanced_upscale_settings({
            "style_profile": "anime", "random_seed": False, "seed": 1,
        })

        self.assertEqual(detail["style_profile"], "realistic_detail")
        self.assertEqual((detail["strength"], detail["grain"], detail["latent_noise_scale"]), (0.7, 0.2, 0.0))
        self.assertEqual((soft["strength"], soft["grain"], soft["latent_noise_scale"]), (0.7, 0.0, 0.002))
        self.assertIn("natural skin", soft["prompt"])
        self.assertEqual((anime["strength"], anime["grain"], anime["latent_noise_scale"]), (0.6, 0.0, 0.0))
        self.assertIn("source image as the sole visual reference", anime["prompt"])
        self.assertIn("Do not add outlines", anime["prompt"])
        self.assertNotIn("cel", anime["prompt"].lower())

        with self.assertRaises(HTTPException):
            app_module.normalize_enhanced_upscale_settings({"style_profile": "automatic"})

    def test_style_profile_graph_overrides_prompt_and_seedvr_softening(self) -> None:
        settings = app_module.normalize_enhanced_upscale_settings({
            "style_profile": "realistic_soft", "strength": 0.55, "grain": 0,
            "random_seed": False, "seed": 2,
        })
        with patch.object(Path, "is_file", return_value=True):
            graph, _ = app_module.build_enhanced_upscale_graph(settings, "mobile_uploads/test.png")

        self.assertEqual(graph["223"]["inputs"]["strength_model"], 0.55)
        self.assertEqual(graph["211"]["inputs"]["model"], ["223", 0])
        self.assertEqual(graph["199"]["inputs"]["grain_power"], 0)
        self.assertEqual(graph["139"]["inputs"]["latent_noise_scale"], 0.002)
        self.assertEqual(graph["201"]["inputs"]["prompt"], settings["prompt"])

        settings.pop("latent_noise_scale")
        with patch.object(Path, "is_file", return_value=True):
            legacy_graph, _ = app_module.build_enhanced_upscale_graph(settings, "mobile_uploads/test.png")
        self.assertEqual(legacy_graph["139"]["inputs"]["latent_noise_scale"], 0.0)

    def test_missing_optional_consistency_lora_bypasses_only_its_loader(self) -> None:
        settings = app_module.normalize_enhanced_upscale_settings({
            "random_seed": False, "seed": 3,
        })
        with tempfile.TemporaryDirectory() as temporary:
            original_comfy = app_module.COMFY_ROOT
            app_module.COMFY_ROOT = Path(temporary) / "ComfyUI"
            required = (
                app_module.COMFY_ROOT / "models" / "diffusion_models" / Path(app_module.ENHANCED_UPSCALE_MODEL),
                app_module.COMFY_ROOT / "models" / "text_encoders" / Path(app_module.ENHANCED_UPSCALE_TEXT_ENCODER),
                app_module.COMFY_ROOT / "models" / "vae" / Path(app_module.ENHANCED_UPSCALE_VAE),
                app_module.COMFY_ROOT / "models" / "SEEDVR2" / Path(app_module.ENHANCED_UPSCALE_SEEDVR_DIT),
                app_module.COMFY_ROOT / "models" / "SEEDVR2" / Path(app_module.ENHANCED_UPSCALE_SEEDVR_VAE),
            )
            for model in required:
                model.parent.mkdir(parents=True, exist_ok=True)
                model.touch()
            try:
                graph, _ = app_module.build_enhanced_upscale_graph(settings, "mobile_uploads/test.png")
            finally:
                app_module.COMFY_ROOT = original_comfy

        self.assertNotIn("223", graph)
        self.assertEqual(graph["211"]["inputs"]["model"], ["4", 0])
        self.assertEqual(graph["201"]["inputs"]["clip"], ["5", 0])
        self.assertEqual(graph["139"]["inputs"]["dit"], ["132", 0])

    def test_ui_exposes_and_submits_flux_style_profiles(self) -> None:
        html = (app_module.STATIC_SOURCE_DIR / "index.html").read_text(encoding="utf-8")
        script = (app_module.STATIC_SOURCE_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="flux-upscale-style-profile"', html)
        self.assertIn('<option value="realistic_soft">写实柔和</option>', html)
        self.assertIn('<option value="anime">二次元 / 插画</option>', html)
        self.assertIn("const FLUX_UPSCALE_STYLE_PROFILES=", script)
        self.assertIn("style_profile:$('#flux-upscale-style-profile').value", script)
        self.assertIn("style_profile:styleProfile.value", script)
        self.assertIn("styleProfile.onchange=()=>applyFluxUpscaleProfile", script)
        self.assertIn('id="flux-upscale-strength"', html)
        self.assertIn("strength:Number($('#flux-upscale-strength').value)", script)

    def test_film_grain_can_be_completely_disabled(self) -> None:
        settings = app_module.normalize_enhanced_upscale_settings({
            "grain": 0, "random_seed": False, "seed": 1,
        })
        self.assertEqual(settings["grain"], 0)
        with patch.object(Path, "is_file", return_value=True):
            graph, _ = app_module.build_enhanced_upscale_graph(settings, "mobile_uploads/test.png")
        self.assertEqual(graph["199"]["inputs"]["grain_power"], 0)
        self.assertEqual(graph["199"]["inputs"]["grain_scale"], 0.1)
        with self.assertRaises(HTTPException):
            app_module.normalize_enhanced_upscale_settings({"grain": -0.01})


class EnhancedUpscaleLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.originals = {name: getattr(app_module, name) for name in ("DATABASE", "COMFY_ROOT", "UPLOAD_DIR", "OUTPUT_DIR", "MOBILE_OUTPUT_DIR")}
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

    def insert_group(self) -> tuple[str, str]:
        output = {"filename": "edit.png", "subfolder": "mobile/qwen2511-modular-flux2", "type": "output"}
        path = app_module.OUTPUT_DIR / output["subfolder"] / output["filename"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"edit")
        items = [{"id": "item", "prompt_id": "edit-prompt", "final": output, "stage1": None}]
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,items_json,storage_scope) VALUES(?,?,?,?,?,?,'mobile')",
                ("group", "qwen2511-modular-flux2", "edit", "completed", app_module.utc_now(), json.dumps(items)),
            )
        return "group", "item"

    def test_seedvr2_and_flux2_results_coexist(self) -> None:
        group_id, item_id = self.insert_group()
        seed_output = {"filename": "seed.png", "subfolder": "mobile/upscale", "type": "output"}
        enhanced_output = {"filename": "enhanced.png", "subfolder": "mobile/enhanced-upscale", "type": "output"}
        for output in (seed_output, enhanced_output):
            path = app_module.OUTPUT_DIR / output["subfolder"] / output["filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(output["filename"].encode())
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO upscale_jobs(upscale_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,output_json,engine) VALUES(?,?,?,?,?,?,?,?)",
                ("seed", "seed-prompt", group_id, item_id, "completed", "2026-01-01", json.dumps(seed_output), "seedvr2"),
            )
            db.execute(
                "INSERT INTO upscale_jobs(upscale_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,engine) VALUES(?,?,?,?,?,?,?)",
                ("enhanced", "enhanced-prompt", group_id, item_id, "running", "2026-01-02", "flux2"),
            )
        history = {"enhanced-prompt": {"status": {"status_str": "success"}, "outputs": {"143": {"images": [enhanced_output]}}}}
        with patch.object(app_module, "comfy_json", new=AsyncMock(return_value=history)):
            result = asyncio.run(app_module.refresh_upscale("enhanced"))
        self.assertEqual(result["engine"], "flux2")
        self.assertIsNotNone(app_module.db_upscale("seed"))
        self.assertIsNotNone(app_module.db_upscale("enhanced"))
        self.assertTrue((app_module.OUTPUT_DIR / seed_output["subfolder"] / seed_output["filename"]).is_file())

    def test_standalone_flux_upscale_keeps_source_until_deleted(self) -> None:
        encoded = BytesIO()
        Image.new("RGB", (16, 12), "white").save(encoded, format="PNG")
        upload = UploadFile(
            BytesIO(encoded.getvalue()), filename="source.png",
            headers=Headers({"content-type": "image/png"}),
        )
        with patch.object(app_module, "build_enhanced_upscale_graph", return_value=({}, {})), patch.object(
            app_module, "comfy_json", new=AsyncMock(return_value={"prompt_id": "flux-prompt"}),
        ):
            submitted = asyncio.run(app_module.submit_standalone_flux_upscale(
                settings=json.dumps({"resolution": 2048, "random_seed": False, "seed": 7}), image=upload,
            ))
        row = app_module.db_upscale(submitted["id"])
        self.assertEqual(row["source_kind"], "standalone")
        source = json.loads(row["source_image_json"])
        source_path = app_module.safe_source_path(source)
        self.assertTrue(source_path.is_file())

        output = {"filename": "standalone.png", "subfolder": "mobile/enhanced-upscale", "type": "output"}
        output_path = app_module.OUTPUT_DIR / output["subfolder"] / output["filename"]
        output_path.parent.mkdir(parents=True)
        output_path.write_bytes(b"result")
        history = {"flux-prompt": {"status": {"status_str": "success"}, "outputs": {"143": {"images": [output]}}}}
        with patch.object(app_module, "comfy_json", new=AsyncMock(return_value=history)):
            completed = asyncio.run(app_module.refresh_upscale(submitted["id"]))
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(source_path.is_file())

        asyncio.run(app_module.delete_upscale(submitted["id"]))
        self.assertFalse(source_path.exists())
        self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
