from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

import mobile_server.app as app_module
from mobile_server.workflow_compiler import get_spec


class MiniMaxH3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.original_comfy_root = app_module.COMFY_ROOT
        self.original_database = app_module.DATABASE
        app_module.COMFY_ROOT = Path(self.temporary.name) / "ComfyUI"
        app_module.DATABASE = Path(self.temporary.name) / "test.sqlite3"
        app_module.init_database()
        for folder in ("diffusion_models", "text_encoders", "vae", "loras"):
            (app_module.COMFY_ROOT / "models" / folder).mkdir(parents=True)
        for key, filename in app_module.H3_MODELS.items():
            folder = "diffusion_models" if key == "diffusion" else "text_encoders" if key == "text_encoder" else "vae"
            (app_module.COMFY_ROOT / "models" / folder / filename).touch()
        for filename in app_module.H3_DIFFUSION_MODELS.values():
            (app_module.COMFY_ROOT / "models" / "diffusion_models" / filename).touch()
        for filename in app_module.H3_TURBO_LORAS:
            (app_module.COMFY_ROOT / "models" / "loras" / filename).touch()
        self.effect_loras = (
            "minimax_h3/cinematic_motion.safetensors",
            "minimax_h3/soft_lighting.safetensors",
        )
        effect_dir = app_module.COMFY_ROOT / "models" / "loras" / "minimax_h3"
        effect_dir.mkdir()
        for filename in self.effect_loras:
            (app_module.COMFY_ROOT / "models" / "loras" / filename).touch()
        self.spec = get_spec("minimax-h3")
        self.descriptor = app_module.workflow_descriptor(self.spec)

    def tearDown(self) -> None:
        app_module.COMFY_ROOT = self.original_comfy_root
        app_module.DATABASE = self.original_database
        self.temporary.cleanup()

    def settings(self, **overrides):
        raw = {**self.descriptor["defaults"], **overrides}
        values = app_module.normalize_settings(self.spec, json.dumps(raw))
        values["prompt"] = "A cinematic ocean sunrise with natural wave and wind audio."
        return values

    def test_safe_defaults_and_absolute_caps(self) -> None:
        defaults = self.descriptor["defaults"]
        self.assertEqual((defaults["megapixels"], defaults["duration"], defaults["steps"]), (0.6, 5, 6))
        self.assertEqual(defaults["turbo_lora"], app_module.H3_TURBO_LORAS[0])
        self.assertEqual(defaults["h3_model"], "original_int8")
        self.assertEqual({item["value"] for item in self.descriptor["model_options"]}, set(app_module.H3_DIFFUSION_MODELS))
        self.assertEqual(
            [item["label"] for item in self.descriptor["model_options"]],
            ["H3 Original INT8"],
        )
        self.assertEqual(self.descriptor["max_lora_slots"], 10)
        self.assertEqual(set(self.descriptor["loras"]), set(self.effect_loras))
        self.assertNotIn("mode", [group["id"] for group in self.descriptor["groups"]])
        self.assertEqual(defaults["mode"], "t2v")
        values = self.settings()
        self.assertEqual((values["width"], values["height"], values["frames"]), (1056, 608, 124))
        self.assertIn(values["estimate_source"], {"completed_h3_history", "no_completed_h3_history"})
        self.assertEqual(values["runtime_profile_version"], app_module.H3_RUNTIME_PROFILE_VERSION)
        self.assertEqual(values["vram"]["risk"], "low")
        if values["estimate_source"] == "completed_h3_history":
            self.assertIsInstance(values["estimated_seconds"], int)
            self.assertEqual(len(values["estimate_range"]), 2)
        else:
            self.assertIsNone(values["estimated_seconds"])
            self.assertEqual(values["estimate_range"], [])

    def test_maximum_standard_combination_is_allowed_without_a_formula_budget(self) -> None:
        values = self.settings(megapixels=0.7, duration=8, steps=8)
        self.assertIn(values["estimate_source"], {"completed_h3_history", "no_completed_h3_history"})

    def test_over_cap_combination_is_rejected_before_queueing(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.settings(megapixels=0.8, duration=8, steps=8)
        self.assertEqual(caught.exception.status_code, 400)

    def test_admin_receives_higher_limits_without_queue_budget_gate(self) -> None:
        token = app_module.CURRENT_USER.set({"user_id": 1, "role": "admin"})
        try:
            descriptor = app_module.workflow_descriptor(self.spec)
            self.assertEqual(descriptor["performance"]["tier"], "admin")
            self.assertEqual(
                (
                    descriptor["performance"]["maximum_megapixels"],
                    descriptor["performance"]["maximum_duration"],
                    descriptor["performance"]["maximum_steps"],
                ),
                (1.0, 15, 12),
            )
            raw = {**descriptor["defaults"], "megapixels": 1.0, "duration": 10, "steps": 8}
            values = app_module.normalize_settings(self.spec, json.dumps(raw))
            self.assertEqual(values["performance_tier"], "admin")
            self.assertEqual((values["width"], values["height"]), (1376, 768))
            self.assertIsNone(values["queue_budget_seconds"])
            self.assertEqual(values["hard_timeout_seconds"], 3600)
            maximum = app_module.normalize_settings(
                self.spec,
                json.dumps({**descriptor["defaults"], "megapixels": 1.0, "duration": 15, "steps": 12}),
            )
            self.assertIn(maximum["estimate_source"], {"completed_h3_history", "no_completed_h3_history"})
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_t2v_graph_uses_turbo_sage_and_native_video(self) -> None:
        graph, _ = app_module.build_graph(self.spec, self.settings(), None)
        self.assertEqual(graph["127"]["inputs"]["unet_name"], app_module.H3_MODELS["diffusion"])
        self.assertEqual(graph["134"]["inputs"]["lora_name"], app_module.H3_TURBO_LORAS[0])
        self.assertEqual(graph["136"]["class_type"], "MiniMaxH3MemoryEfficientSageAttentionPatch")
        self.assertEqual(graph["126"]["inputs"]["model"], ["136", 0])
        self.assertEqual(graph["92"]["class_type"], "SaveVideo")
        self.assertNotIn("first_frame", graph["131"]["inputs"])

    def test_unknown_h3_model_is_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            self.settings(h3_model="not-a-model")

    def test_runtime_setting_summary_uses_public_model_and_mode_names(self) -> None:
        summary = app_module.h3_fingerprint_summary({
            "h3_model": "original_int8", "mode": "i2v",
            "width": 1056, "height": 608, "frames": 124, "steps": 8,
        })
        self.assertIn("H3 Original INT8", summary)
        self.assertIn("图生视频", summary)
        self.assertNotIn("original_int8", summary)

    def test_effect_loras_stack_before_turbo_and_accept_lokr(self) -> None:
        values = self.settings(loras=[
            {"name": self.effect_loras[0], "weight": 0.8},
            {"name": self.effect_loras[1], "weight": 1.1},
        ])
        self.assertEqual(len(values["loras"]), 2)
        graph, _ = app_module.build_graph(self.spec, values, None)
        self.assertEqual(graph["h3_effect_lora_0"]["inputs"]["model"], ["127", 0])
        self.assertEqual(graph["h3_effect_lora_1"]["inputs"]["model"], ["h3_effect_lora_0", 0])
        self.assertEqual(graph["h3_effect_lora_0"]["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(
            graph["h3_effect_lora_0"]["inputs"]["lora_name"],
            str(Path("minimax_h3") / "cinematic_motion.safetensors"),
        )
        self.assertEqual(
            graph["h3_effect_lora_1"]["inputs"]["lora_name"],
            str(Path("minimax_h3") / "soft_lighting.safetensors"),
        )
        self.assertEqual(graph["h3_effect_lora_0"]["inputs"]["strength_model"], 0.8)
        self.assertEqual(graph["134"]["inputs"]["model"], ["h3_effect_lora_1", 0])
        self.assertEqual(graph["136"]["inputs"]["model"], ["134", 0])

    def test_ten_effect_loras_are_allowed_and_all_reach_the_turbo_loader(self) -> None:
        effect_dir = app_module.COMFY_ROOT / "models" / "loras" / "minimax_h3"
        for index in range(3, 11):
            (effect_dir / f"effect_{index}.safetensors").touch()
        descriptor = app_module.workflow_descriptor(self.spec)
        available = descriptor["loras"][:10]
        self.assertEqual(len(available), 10)
        values = self.settings(loras=[
            {"name": name, "weight": index / 10}
            for index, name in enumerate(available, 1)
        ])
        graph, _ = app_module.build_graph(self.spec, values, None)
        self.assertEqual(graph["134"]["inputs"]["model"], ["h3_effect_lora_9", 0])
        self.assertEqual(graph["h3_effect_lora_9"]["inputs"]["model"], ["h3_effect_lora_8", 0])

    def test_comfy_validation_error_identifies_rejected_lora(self) -> None:
        detail = app_module.comfy_rejection_detail({
            "error": {"message": "Prompt outputs failed validation"},
            "node_errors": {
                "h3_effect_lora_0": {
                    "class_type": "LoraLoaderModelOnly",
                    "errors": [{
                        "type": "value_not_in_list",
                        "message": "Value not in list",
                        "extra_info": {
                            "input_name": "lora_name",
                            "received_value": "minimax_h3/example_style.safetensors",
                        },
                    }],
                },
            },
        })
        self.assertIn("LoraLoaderModelOnly.lora_name", detail)
        self.assertIn("example_style.safetensors", detail)

    def test_i2v_graph_requires_and_loads_first_frame(self) -> None:
        values = self.settings(mode="i2v", megapixels=0.7, aspect_ratio="1:1")
        self.assertEqual(values["aspect_ratio"], "source")
        self.assertEqual(values["megapixels"], 0.7)
        with self.assertRaises(HTTPException):
            app_module.build_graph(self.spec, values, None)
        graph, _ = app_module.build_graph(self.spec, values, "mobile_uploads/frame.png")
        self.assertEqual(graph["140"]["class_type"], "LoadImage")
        self.assertEqual(graph["131"]["inputs"]["first_frame"], ["140", 0])

    def test_i2v_dimensions_follow_source_ratio_on_h3_grid(self) -> None:
        for source, expected in (
            ((1920, 1080), (1024, 576)),
            ((1080, 1920), (576, 1024)),
            ((1200, 1200), (768, 768)),
            ((1600, 1200), (896, 672)),
        ):
            width, height = app_module.h3_source_dimensions(*source, 0.6)
            self.assertEqual((width, height), expected)
            self.assertEqual(width % 32, 0)
            self.assertEqual(height % 32, 0)
            self.assertLess(abs(width / height / (source[0] / source[1]) - 1), 0.025)

    def test_h3_public_parameters_preserve_actual_seed_precision(self) -> None:
        parameters = app_module.public_parameters({
            "workflow_key": "minimax-h3",
            "parameters_json": json.dumps({"generation_seeds": [9223372036854775807]}),
        })
        self.assertEqual(parameters["generation_seeds"], ["9223372036854775807"])

    def test_frontends_choose_h3_mode_at_entry_and_step_one(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for bundle in (root / "static",):
            html = (bundle / "index.html").read_text(encoding="utf-8")
            script = (bundle / "app.js").read_text(encoding="utf-8")
            self.assertIn('id="h3-mode-field"', html)
            self.assertIn('name="mode" type="hidden"', html)
            self.assertIn('data-h3-mode="t2v"', html)
            self.assertIn('data-h3-mode="i2v"', html)
            self.assertIn('id="reuse-qwen"', html)
            self.assertIn('id="reuse-krea"', html)
            self.assertIn('id="reuse-h3"', html)
            self.assertIn("MiniMax H3 文生视频", script)
            self.assertIn("MiniMax H3 图生视频", script)
            self.assertTrue("mode:'t2v'" in script or "&mode=t2v" in script)
            self.assertTrue("mode:'i2v'" in script or "&mode=i2v" in script)
            self.assertIn("function setH3Mode(mode,updateUrl=true)", script)
            self.assertIn("function syncH3AspectRatioControl()", script)
            self.assertIn("field.hidden=followsSource", script)
            self.assertIn("for(const name of ['aspect_ratio'])", script)
            self.assertIn("function h3VideoPosterUrl(job,item)", script)
            self.assertIn("'minimax-h3'", script)
            self.assertIn("video-play-mark", script)
            self.assertIn("function mountHistoryVideoPoster(media,job,item)", script)
            self.assertNotIn("function mountNativeHistoryVideo(media,job,item)", script)
            self.assertNotIn("media.removeAttribute('href')", script)
            self.assertNotIn("delete media.dataset.nav", script)
            self.assertIn("media.style.aspectRatio=", script)
            self.assertIn("history-video-card", script)
            self.assertIn("function saveH3Video(job,item)", script)
            self.assertIn("保存/下载", script)
            self.assertIn("continue_target", script)
            self.assertIn("target.optional_image", script)
            self.assertIn("video-result-detail", script)
            self.assertIn("link=image?.closest('a')", script)
            self.assertIn("if(link)link.replaceWith(video)", script)
            self.assertIn("$('#detail-interrogate-source')?.remove();", script)
            self.assertIn("detail-delete-job", script)
            self.assertIn("generation_seeds", script)
            self.assertIn("Unable to restore MiniMax H3 seed", script)
            self.assertIn("删除此任务", script)
            self.assertIn("/api/tasks/ws", script)
            self.assertIn("function startTaskEvents()", script)
            self.assertNotIn("setInterval(loadActivity,2200)", script)
            self.assertIn("function updateH3PerformanceNote", script)
            self.assertIn("/api/workflows/minimax-h3/estimate", script)
        chat_script = (root / "static" / "chat.js").read_text(encoding="utf-8")
        self.assertIn("/poster?preview=512", chat_script)
        self.assertIn("chat-share-video-preview", chat_script)
        interface_script = (root / "static" / "interface.js").read_text(encoding="utf-8")
        interface_style = (root / "static" / "interface.css").read_text(encoding="utf-8")
        self.assertIn("body.scrollHeight <= body.clientHeight + 1", interface_script)
        self.assertIn("flex: 0 0 auto", interface_style)


if __name__ == "__main__":
    unittest.main()
