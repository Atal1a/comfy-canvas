from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import mobile_server.app as app
from mobile_server.config import DEFAULT_WORKFLOWS, load_settings
from mobile_server.workflow_compiler import enabled_workflows, get_spec


class KreaTurboTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(app, "COMFY_ROOT", self.root)
        self.root_patch.start()
        for folder, name in (
            ("diffusion_models", app.SETTINGS.krea_turbo_model),
            ("text_encoders", app.SETTINGS.krea_turbo_text_encoder),
            ("vae", app.SETTINGS.krea_turbo_vae),
        ):
            path = self.root / "models" / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.spec = get_spec("krea-turbo")

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def settings(self, **updates):
        values = {**app.workflow_descriptor(self.spec)["defaults"], **updates}
        result = app.normalize_settings(self.spec, json.dumps(values))
        result["prompt"] = "A ceramic vase in soft morning light"
        return result

    def test_default_workflow_is_text_only_with_ten_slots(self):
        self.assertIn("krea-turbo", DEFAULT_WORKFLOWS)
        self.assertIn(self.spec, enabled_workflows())
        descriptor = app.workflow_descriptor(self.spec)
        self.assertFalse(descriptor["requires_image"])
        self.assertEqual(descriptor["max_lora_slots"], 10)
        self.assertEqual(descriptor["defaults"]["negative_prompt"], "")
        self.assertEqual(descriptor["default_loras"], [])

    def test_no_reference_or_lora_is_required(self):
        graph, _ = app.build_graph(self.spec, self.settings(), None)
        types = [node["class_type"] for node in graph.values()]
        self.assertNotIn("LoadImage", types)
        self.assertNotIn("LoraLoaderModelOnly", types)
        self.assertNotIn("ResolutionSelector", types)
        self.assertEqual(graph["4"]["inputs"]["text"], "A ceramic vase in soft morning light")
        self.assertEqual(graph["1"]["inputs"]["unet_name"], app.SETTINGS.krea_turbo_model)
        self.assertEqual(graph["11"]["inputs"]["model"], ["1", 0])
        self.assertEqual(graph["11"]["inputs"]["steps"], 8)

    def test_ten_loras_are_connected_in_order(self):
        names = [f"style_{i}.safetensors" for i in range(10)]
        with patch.object(app, "lora_options", return_value=names):
            values = self.settings(loras=[{"name": n, "weight": 0.25 + i / 20} for i, n in enumerate(names)])
        graph, _ = app.build_graph(self.spec, values, None)
        node = graph["11"]["inputs"]["model"][0]
        chain = []
        while graph[node]["class_type"] == "LoraLoaderModelOnly":
            inputs = graph[node]["inputs"]
            chain.append((inputs["lora_name"], inputs["strength_model"]))
            node = inputs["model"][0]
        self.assertEqual(list(reversed(chain)), [(n, 0.25 + i / 20) for i, n in enumerate(names)])
        self.assertEqual(node, "1")

    def test_excess_or_unknown_loras_are_rejected(self):
        with patch.object(app, "lora_options", return_value=["style.safetensors"]):
            with self.assertRaises(HTTPException):
                self.settings(loras=[{"name": "style.safetensors"}] * 11)
            with self.assertRaises(HTTPException):
                self.settings(loras=[{"name": "missing.safetensors"}])

    def test_dimensions_and_negative_prompt(self):
        values = self.settings(aspect_ratio="16:9 (Widescreen)", negative_prompt="blur", seed=42)
        graph, _ = app.build_graph(self.spec, values, None)
        size = graph["7"]["inputs"]
        self.assertGreater(size["width"], size["height"])
        self.assertEqual(size["width"] % values["multiple"], 0)
        self.assertEqual(graph["11"]["inputs"]["seed"], 42)
        self.assertEqual(graph["turbo_negative"]["inputs"]["text"], "blur")
        self.assertEqual(graph["11"]["inputs"]["negative"], ["turbo_negative", 0])

    def test_missing_model_is_reported(self):
        (self.root / "models" / "diffusion_models" / app.SETTINGS.krea_turbo_model).unlink()
        with self.assertRaises(HTTPException) as error:
            app.build_graph(self.spec, self.settings(), None)
        self.assertEqual(error.exception.status_code, 409)

    def test_existing_identity_model_config_is_reused(self):
        config = self.root / "config.toml"
        config.write_text('[models.krea_identity]\nbase_model = "existing.safetensors"\n', encoding="utf-8")
        self.assertEqual(load_settings(config).krea_turbo_model, "existing.safetensors")
