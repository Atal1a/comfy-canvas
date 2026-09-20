from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import mobile_server.app as app_module
from mobile_server.app import build_graph, normalize_settings, workflow_descriptor
from mobile_server.workflow_compiler import get_spec, is_two_stage_key


ROOT = Path(__file__).resolve().parents[2]


class ModularFlux2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_comfy_root = app_module.COMFY_ROOT
        app_module.COMFY_ROOT = Path(self.temporary.name) / "ComfyUI"
        configured_files = {
            "diffusion_models": app_module.SETTINGS.qwen2511_model,
            "text_encoders": app_module.SETTINGS.qwen2511_text_encoder,
            "vae": app_module.SETTINGS.qwen2511_vae,
            "loras": app_module.SETTINGS.qwen2511_lightning_lora,
        }
        for folder, filename in configured_files.items():
            path = app_module.COMFY_ROOT / "models" / folder / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.qwen_loras = [f"test_qwen_{index}.safetensors" for index in range(1, 11)]
        self.lora_patch = patch.object(
            app_module,
            "lora_options",
            side_effect=lambda key: list(self.qwen_loras)
            if key == "qwen2511-modular-flux2"
            else [],
        )
        self.lora_patch.start()

    def tearDown(self) -> None:
        self.lora_patch.stop()
        app_module.COMFY_ROOT = self.original_comfy_root
        self.temporary.cleanup()

    def test_canonical_mobile_workflow_is_neutral_and_edit_only(self) -> None:
        source = ROOT / "mobile_server" / "workflows" / "qwen2511-modular-flux2.mobile.json"
        graph = json.loads(source.read_text(encoding="utf-8"))
        nodes = {str(node["id"]): node for node in graph["nodes"]}
        self.assertGreaterEqual(graph["extra"]["mobile_server_version"], 12)
        self.assertEqual(nodes["430"]["widgets_values"][0], "qwen_image_edit_2511_fp8_e4m3fn_scaled.safetensors")
        self.assertEqual(nodes["440"]["widgets_values"][0], app_module.SETTINGS.qwen2511_lightning_lora)
        for node_id in ("441", "442", "443", "450", "451", "452"):
            self.assertEqual(nodes[node_id]["widgets_values"], ["None", 1.0])
        self.assertNotIn("453", nodes)
        self.assertNotIn("426", nodes)
        self.assertEqual(nodes["425"]["widgets_values"][0], "mobile/qwen2511-modular-flux2/Qwen2511_Edit")

    def test_selected_user_loras_compile_and_unused_slots_are_bypassed(self) -> None:
        spec = get_spec("qwen2511-modular-flux2")
        descriptor = workflow_descriptor(spec)
        selected = descriptor["loras"][:3]
        values = {
            **descriptor["defaults"],
            "loras": [
                {"name": selected[0], "weight": 0.65},
                {"name": selected[1], "weight": 0.4},
                {"name": selected[2], "weight": 0.3},
            ],
        }
        settings = normalize_settings(spec, json.dumps(values))
        settings["prompt"] = "keep identity and edit clothing color"
        api, _ = build_graph(spec, settings, "mobile_uploads/test.png")
        self.assertTrue(is_two_stage_key(spec.key))
        self.assertEqual(api["440"]["inputs"]["lora_name"], app_module.SETTINGS.qwen2511_lightning_lora)
        self.assertEqual(api["441"]["inputs"]["lora_name"], selected[0])
        self.assertEqual(api["443"]["inputs"]["lora_name"], selected[2])
        self.assertEqual(api["435"]["inputs"]["image1"], ["78", 0])
        self.assertEqual(api["444"]["inputs"]["string_b"], settings["prompt"])
        self.assertEqual(api["425"]["inputs"]["images"], ["438", 0])
        self.assertFalse(any(node["class_type"] in {"Flux2Scheduler", "ColorMatch"} for node in api.values()))

    def test_all_optional_loras_can_be_bypassed(self) -> None:
        spec = get_spec("qwen2511-modular-flux2")
        settings = normalize_settings(spec, json.dumps(workflow_descriptor(spec)["defaults"]))
        settings["prompt"] = "keep identity"
        api, _ = build_graph(spec, settings, "mobile_uploads/test.png")
        for node_id in ("441", "442", "443", "450", "451", "452"):
            self.assertNotIn(node_id, api)
        self.assertEqual(api["428"]["inputs"]["model"], ["440", 0])

    def test_qwen_user_lora_weights_allow_plus_or_minus_fifteen(self) -> None:
        spec = get_spec("qwen2511-modular-flux2")
        descriptor = workflow_descriptor(spec)
        values = {
            **descriptor["defaults"],
            "loras": [
                {"name": descriptor["loras"][0], "weight": -15},
                {"name": descriptor["loras"][1], "weight": 15},
            ],
        }
        settings = normalize_settings(spec, json.dumps(values))
        self.assertEqual([item["weight"] for item in settings["stage1_loras"]], [-15, 15])

    def test_ten_qwen_loras_continue_after_the_six_editor_slots(self) -> None:
        spec = get_spec("qwen2511-modular-flux2")
        descriptor = workflow_descriptor(spec)
        loras = [
            {"name": name, "weight": index / 10}
            for index, name in enumerate(descriptor["loras"][:10], 1)
        ]
        settings = normalize_settings(spec, json.dumps({**descriptor["defaults"], "loras": loras}))
        settings["prompt"] = "keep identity"
        api, _ = build_graph(spec, settings, "mobile_uploads/test.png")
        for slot, item in enumerate(loras[6:], 7):
            node_id = f"qwen2511_extra_lora_{slot}"
            previous = "452" if slot == 7 else f"qwen2511_extra_lora_{slot - 1}"
            self.assertEqual(api[node_id]["inputs"]["model"], [previous, 0])
            self.assertEqual(api[node_id]["inputs"]["lora_name"], item["name"])
            self.assertEqual(api[node_id]["inputs"]["strength_model"], item["weight"])
        self.assertEqual(api["428"]["inputs"]["model"], ["qwen2511_extra_lora_10", 0])
        with self.assertRaises(HTTPException):
            normalize_settings(
                spec,
                json.dumps({**descriptor["defaults"], "loras": [*loras, loras[0]]}),
            )

    def test_descriptor_has_no_default_user_lora_or_upscale_setting(self) -> None:
        descriptor = workflow_descriptor(get_spec("qwen2511-modular-flux2"))
        self.assertEqual(descriptor["max_lora_slots"], 10)
        self.assertEqual(descriptor["default_loras"], [])
        self.assertEqual(descriptor["kind"], "image_edit")
        self.assertFalse(descriptor["two_stage"])
        self.assertFalse(any(name.startswith("stage2_") for name in descriptor["defaults"]))
        self.assertNotIn("final_upscale_enabled", descriptor["defaults"])


if __name__ == "__main__":
    unittest.main()
