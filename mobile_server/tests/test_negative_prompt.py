from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

import mobile_server.app as app_module
from mobile_server.app import build_graph, get_spec, normalize_settings, workflow_descriptor


class NegativePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_comfy_root = app_module.COMFY_ROOT
        app_module.COMFY_ROOT = Path(self.temporary.name) / "ComfyUI"
        diffusion = app_module.COMFY_ROOT / "models" / "diffusion_models"
        loras = app_module.COMFY_ROOT / "models" / "loras"
        diffusion.mkdir(parents=True)
        loras.mkdir(parents=True)
        for filename in app_module.KREA_MODELS.values():
            (diffusion / filename).touch()
        (loras / app_module.KREA_IDENTITY_EDIT_LORA).touch()
        for folder, filename in {
            "text_encoders": app_module.SETTINGS.krea_identity_text_encoder,
            "vae": app_module.SETTINGS.krea_identity_vae,
        }.items():
            path = app_module.COMFY_ROOT / "models" / folder / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        for folder, filename in {
            "diffusion_models": app_module.SETTINGS.qwen2511_model,
            "text_encoders": app_module.SETTINGS.qwen2511_text_encoder,
            "vae": app_module.SETTINGS.qwen2511_vae,
            "loras": app_module.SETTINGS.qwen2511_lightning_lora,
        }.items():
            path = app_module.COMFY_ROOT / "models" / folder / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def tearDown(self) -> None:
        app_module.COMFY_ROOT = self.original_comfy_root
        self.temporary.cleanup()

    def settings(self, key: str, negative: str):
        spec = get_spec(key)
        values = dict(workflow_descriptor(spec)["defaults"])
        values["negative_prompt"] = negative
        settings = normalize_settings(spec, json.dumps(values))
        settings["prompt"] = "positive prompt"
        return spec, settings

    def test_qwen_negative_encoder_is_overridden(self) -> None:
        spec, settings = self.settings("qwen2511-modular-flux2", "exclude artifacts")
        graph, _ = build_graph(spec, settings, "mobile_uploads/test.png")
        self.assertEqual(graph["434"]["inputs"]["prompt"], "exclude artifacts")

    def test_krea_identity_negative_input_is_effective(self) -> None:
        spec, settings = self.settings("krea-identity-edit", "exclude blur")
        graph, _ = build_graph(spec, settings, "mobile_uploads/test.png")
        self.assertEqual(graph["85"]["inputs"]["prompt"], "exclude blur")
        self.assertEqual(graph["53"]["inputs"]["negative"], graph["53"]["inputs"]["positive"])

        settings["stage1_cfg"] = 2
        graph, _ = build_graph(spec, settings, "mobile_uploads/test.png")
        self.assertEqual(graph["53"]["inputs"]["negative"], ["85", 0])

    def test_negative_prompt_length_is_limited(self) -> None:
        spec = get_spec("qwen2511-modular-flux2")
        values = dict(workflow_descriptor(spec)["defaults"])
        values["negative_prompt"] = "x" * 2001
        with self.assertRaises(HTTPException):
            normalize_settings(spec, json.dumps(values))

    def test_all_workflows_start_without_negative_prompt(self) -> None:
        for spec in app_module.WORKFLOWS:
            with self.subTest(workflow=spec.key):
                self.assertEqual(workflow_descriptor(spec)["defaults"]["negative_prompt"], "")

    def test_omitted_negative_prompt_compiles_as_empty(self) -> None:
        for key, node in (("qwen2511-modular-flux2", "434"), ("krea-identity-edit", "85")):
            with self.subTest(workflow=key):
                spec = get_spec(key)
                settings = normalize_settings(spec, "{}")
                settings["prompt"] = "positive prompt"
                self.assertEqual(settings["negative_prompt"], "")
                graph, _ = build_graph(spec, settings, "mobile_uploads/test.png")
                self.assertEqual(graph[node]["inputs"]["prompt"], "")


if __name__ == "__main__":
    unittest.main()
