from __future__ import annotations

import json
from pathlib import Path
import struct
import tempfile
import unittest

from fastapi import HTTPException
from PIL import Image

import mobile_server.app as app_module
from mobile_server.workflow_compiler import get_spec


ROOT = Path(__file__).resolve().parents[2]


def write_krea_lora(path: Path) -> None:
    header = json.dumps({
        "__metadata__": {"ss_base_model_version": "krea2"},
        "diffusion_model.blocks.0.fake.weight": {"dtype": "F16", "shape": [0], "data_offsets": [0, 0]},
    }).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(header)) + header)


class KreaIdentityEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_comfy_root = app_module.COMFY_ROOT
        self.original_upload_dir = app_module.UPLOAD_DIR
        app_module.COMFY_ROOT = Path(self.temporary.name) / "ComfyUI"
        app_module.UPLOAD_DIR = app_module.COMFY_ROOT / "input" / "mobile_uploads"
        diffusion = app_module.COMFY_ROOT / "models" / "diffusion_models"
        loras = app_module.COMFY_ROOT / "models" / "loras"
        diffusion.mkdir(parents=True)
        loras.mkdir(parents=True)
        for filename in app_module.KREA_MODELS.values():
            (diffusion / filename).touch()
        for folder, filename in {
            "text_encoders": app_module.SETTINGS.krea_identity_text_encoder,
            "vae": app_module.SETTINGS.krea_identity_vae,
        }.items():
            path = app_module.COMFY_ROOT / "models" / folder / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (loras / app_module.KREA_IDENTITY_EDIT_LORA).touch()
        write_krea_lora(loras / "Krea_Test_LoRA.safetensors")
        self.spec = get_spec("krea-identity-edit")
        self.descriptor = app_module.workflow_descriptor(self.spec)

    def tearDown(self) -> None:
        app_module.COMFY_ROOT = self.original_comfy_root
        app_module.UPLOAD_DIR = self.original_upload_dir
        self.temporary.cleanup()

    def settings(self, **changes) -> dict:
        raw = {**self.descriptor["defaults"], **changes}
        values = app_module.normalize_settings(self.spec, json.dumps(raw))
        values["prompt"] = "Keep this person's identity and place them in a new scene."
        return values

    def test_descriptor_defaults_to_official_single_image_edit(self) -> None:
        self.assertTrue(self.descriptor["requires_image"])
        self.assertEqual(self.descriptor["kind"], "image_edit")
        self.assertEqual(self.descriptor["label"], "Krea2 图生图")
        self.assertEqual(self.descriptor["lora_family"], "krea2")
        self.assertEqual(self.descriptor["max_lora_slots"], 10)
        self.assertEqual(self.descriptor["defaults"]["krea_model"], "official_turbo")
        self.assertEqual(self.descriptor["defaults"]["reference_mode"], "edit")
        self.assertEqual(self.descriptor["defaults"]["size_mode"], "source_ratio")
        self.assertEqual(self.descriptor["defaults"]["output_megapixels"], 1.0)
        self.assertEqual(self.descriptor["defaults"]["width"], 1024)
        self.assertEqual(self.descriptor["defaults"]["height"], 1024)
        self.assertEqual(self.descriptor["defaults"]["identity_lora_strength"], 1.0)
        self.assertNotIn("flux_guidance", self.descriptor["defaults"])
        self.assertEqual(self.descriptor["defaults"]["scene_ref_boost"], 1.0)
        self.assertEqual(self.descriptor["defaults"]["fit_mode"], "fit")
        self.assertEqual(
            [item["value"] for item in self.descriptor["model_options"]],
            list(app_module.KREA_MODELS),
        )
        self.assertEqual(
            self.descriptor["defaults"]["negative_prompt"],
            "",
        )
        self.assertNotIn(
            app_module.KREA_IDENTITY_EDIT_LORA,
            {Path(name).name for name in self.descriptor["loras"]},
        )
        self.assertEqual(self.descriptor["loras"], ["Krea_Test_LoRA.safetensors"])

    def test_source_ratio_is_safe_and_processing_copy_preserves_original(self) -> None:
        width, height = app_module.krea_safe_dimensions(1179, 2556)
        self.assertLessEqual(width * height, app_module.KREA_IDENTITY_MAX_PIXELS)
        self.assertAlmostEqual(width / height, 1179 / 2556, delta=0.01)
        one_mp_width, one_mp_height = app_module.krea_safe_dimensions(
            1920, 1080, 1024 * 1024,
        )
        two_mp_width, two_mp_height = app_module.krea_safe_dimensions(
            1920, 1080, 2 * 1024 * 1024,
        )
        self.assertLessEqual(one_mp_width * one_mp_height, 1024 * 1024)
        self.assertEqual((two_mp_width, two_mp_height), (1920, 1080))
        extreme_width, extreme_height = app_module.krea_safe_dimensions(10000, 400)
        self.assertLessEqual(max(extreme_width, extreme_height), 2048)

        source = app_module.COMFY_ROOT / "input" / "mobile_uploads" / "portrait.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1179, 2556), "white").save(source)
        image_name, record = app_module.prepare_krea_identity_source("mobile_uploads/portrait.png")
        processing = app_module.COMFY_ROOT / "input" / image_name
        with Image.open(source) as original:
            self.assertEqual(original.size, (1179, 2556))
        with Image.open(processing) as image:
            self.assertLessEqual(image.width * image.height, app_module.KREA_IDENTITY_MAX_PIXELS)
        self.assertEqual(record["subfolder"], "mobile_uploads")

    def test_graph_has_fixed_identity_lora_and_dual_source_conditioning(self) -> None:
        graph, _ = app_module.build_graph(
            self.spec, self.settings(reference_mode="edit"), "mobile_uploads/source.png",
        )
        self.assertEqual(graph["55"]["inputs"]["unet_name"], app_module.KREA_MODELS["official_turbo"])
        self.assertEqual(graph["56"]["inputs"]["clip_name"], app_module.SETTINGS.krea_identity_text_encoder)
        self.assertEqual(graph["56"]["inputs"]["type"], "krea2")
        self.assertEqual(graph["57"]["inputs"]["vae_name"], app_module.SETTINGS.krea_identity_vae)
        self.assertEqual(graph["71"]["inputs"]["lora_name"], app_module.KREA_IDENTITY_EDIT_LORA)
        self.assertEqual(graph["71"]["inputs"]["strength_model"], 1.0)
        self.assertEqual(graph["79"]["inputs"]["source_latent"], ["82", 0])
        self.assertEqual(graph["79"]["inputs"]["target_latent"], ["82", 0])
        self.assertEqual(graph["79"]["inputs"]["source_image"], ["72", 0])
        self.assertEqual(graph["79"]["inputs"]["vae"], ["57", 0])
        self.assertEqual(graph["79"]["inputs"]["fit_mode"], "fit")
        self.assertEqual(graph["84"]["inputs"]["image"], ["72", 0])
        self.assertEqual(graph["85"]["inputs"]["image"], ["72", 0])
        self.assertEqual(graph["114"]["class_type"], "FluxGuidance")
        self.assertEqual(graph["114"]["inputs"]["conditioning"], ["84", 0])
        self.assertEqual(graph["114"]["inputs"]["guidance"], 3.5)
        self.assertEqual(graph["53"]["inputs"]["positive"], ["114", 0])
        # CFG=1 deliberately reuses positive conditioning and leaves the
        # negative image-grounded encoder unreachable.
        self.assertEqual(graph["53"]["inputs"]["negative"], ["114", 0])
        self.assertEqual(graph["29"]["inputs"]["filename_prefix"], "mobile/krea-identity-edit/Krea_Identity_Edit")

    def test_edit_controls_are_applied_to_the_graph(self) -> None:
        graph, _ = app_module.build_graph(
            self.spec,
            self.settings(
                identity_lora_strength=0.65,
                ref_boost=1.4,
                grounding_px=1024,
                fit_mode="crop (legacy)",
            ),
            "mobile_uploads/source.png",
        )
        self.assertEqual(graph["71"]["inputs"]["strength_model"], 0.65)
        self.assertEqual(graph["79"]["inputs"]["ref_boost"], 1.4)
        self.assertEqual(graph["79"]["inputs"]["ref_boost_a"], 1.0)
        self.assertEqual(graph["84"]["inputs"]["grounding_px"], 1024)
        self.assertEqual(graph["79"]["inputs"]["fit_mode"], "crop (legacy)")

    def test_recommended_identity_controls_are_applied_to_the_graph(self) -> None:
        graph, _ = app_module.build_graph(
            self.spec,
            self.settings(
                reference_mode="identity",
                stage1_steps=12,
                stage1_cfg=1,
                stage1_denoise=1,
                identity_lora_strength=1,
                ref_boost=4,
                grounding_px=1024,
                fit_mode="fit",
            ),
            "mobile_uploads/source.png",
        )
        self.assertEqual(graph["53"]["inputs"]["steps"], 12)
        self.assertEqual(graph["53"]["inputs"]["cfg"], 1)
        self.assertEqual(graph["53"]["inputs"]["denoise"], 1)
        self.assertEqual(graph["71"]["inputs"]["strength_model"], 1)
        self.assertEqual(graph["79"]["inputs"]["ref_boost"], 4)
        self.assertEqual(graph["84"]["inputs"]["grounding_px"], 1024)
        self.assertEqual(graph["79"]["inputs"]["fit_mode"], "fit")

    def test_legacy_face_swap_settings_migrate_to_dual_reference(self) -> None:
        values = {**self.descriptor["defaults"], "reference_mode": "face_swap", "krea_model": "official_turbo"}
        normalized = app_module.normalize_settings(self.spec, json.dumps(values))
        self.assertEqual(normalized["reference_mode"], "dual_reference")

    def test_dual_reference_uses_official_scene_then_subject_wiring(self) -> None:
        graph, _ = app_module.build_graph(
            self.spec,
            self.settings(
                reference_mode="dual_reference",
                ref_boost=4,
                scene_ref_boost=1.25,
                grounding_px=768,
            ),
            "mobile_uploads/scene.png",
            "mobile_uploads/person.png",
        )
        self.assertEqual(graph["72"]["inputs"]["image"], "mobile_uploads/scene.png")
        self.assertEqual(graph["115"]["inputs"]["image"], "mobile_uploads/person.png")
        self.assertNotIn("116", graph)
        self.assertNotIn("source_latent_b", graph["79"]["inputs"])
        self.assertEqual(graph["79"]["inputs"]["source_image"], ["72", 0])
        self.assertEqual(graph["79"]["inputs"]["source_image_b"], ["115", 0])
        self.assertEqual(graph["79"]["inputs"]["target_latent"], ["82", 0])
        self.assertEqual(graph["79"]["inputs"]["ref_boost"], 4)
        self.assertEqual(graph["79"]["inputs"]["ref_boost_a"], 1.25)
        self.assertEqual(graph["84"]["inputs"]["image"], ["72", 0])
        self.assertEqual(graph["84"]["inputs"]["image_b"], ["115", 0])
        self.assertEqual(graph["85"]["inputs"]["image_b"], ["115", 0])

    def test_single_image_modes_remove_the_second_reference_branch(self) -> None:
        for mode in ("edit", "identity"):
            with self.subTest(mode=mode):
                graph, _ = app_module.build_graph(
                    self.spec,
                    self.settings(reference_mode=mode),
                    "mobile_uploads/source.png",
                )
                self.assertNotIn("115", graph)
                self.assertNotIn("116", graph)
                self.assertNotIn("source_latent_b", graph["79"]["inputs"])
                self.assertNotIn("source_image_b", graph["79"]["inputs"])
                self.assertNotIn("image_b", graph["84"]["inputs"])

    def test_identity_reference_mode_changes_grounding_not_source_wiring(self) -> None:
        edit, _ = app_module.build_graph(
            self.spec, self.settings(reference_mode="edit"), "mobile_uploads/source.png",
        )
        identity, _ = app_module.build_graph(
            self.spec, self.settings(reference_mode="identity"), "mobile_uploads/source.png",
        )
        self.assertEqual(edit["84"]["inputs"]["system_prompt"], "")
        self.assertEqual(
            identity["84"]["inputs"]["system_prompt"],
            app_module.KREA_IDENTITY_REFERENCE_SYSTEM_PROMPT,
        )
        self.assertIn("pose, clothing, background", identity["84"]["inputs"]["system_prompt"])
        self.assertEqual(edit["79"]["inputs"]["source_image"], identity["79"]["inputs"]["source_image"])

    def test_user_loras_remain_optional(self) -> None:
        no_optional, _ = app_module.build_graph(
            self.spec, self.settings(), "mobile_uploads/source.png",
        )
        self.assertNotIn("108", no_optional)
        self.assertNotIn("109", no_optional)
        selected = self.descriptor["loras"][0]
        enabled, _ = app_module.build_graph(
            self.spec,
            self.settings(
                loras=[{"name": selected, "weight": 0.7}],
            ),
            "mobile_uploads/source.png",
        )
        self.assertEqual(enabled["109"]["inputs"]["lora_name"], selected)
        self.assertEqual(enabled["71"]["inputs"]["model"], ["109", 0])

    def test_ten_user_loras_are_applied_and_eleventh_is_rejected(self) -> None:
        root = app_module.COMFY_ROOT / "models" / "loras"
        for index in range(2, 11):
            write_krea_lora(root / f"Krea_Test_LoRA_{index}.safetensors")
        selected = app_module.lora_options(self.spec.key)[:10]
        self.assertEqual(len(selected), 10)
        loras = [{"name": name, "weight": index / 10} for index, name in enumerate(selected, 1)]
        values = self.settings(loras=loras)
        graph, _ = app_module.build_graph(
            self.spec, values, "mobile_uploads/source.png",
        )
        for node_id, item in zip(("109", "110", "111", "117", "118", "119"), loras):
            self.assertEqual(graph[node_id]["inputs"]["lora_name"], item["name"])
            self.assertEqual(graph[node_id]["inputs"]["strength_model"], item["weight"])
        for slot, item in enumerate(loras[6:], 7):
            node_id = f"krea_identity_extra_lora_{slot}"
            previous = "119" if slot == 7 else f"krea_identity_extra_lora_{slot - 1}"
            self.assertEqual(graph[node_id]["inputs"]["model"], [previous, 0])
            self.assertEqual(graph[node_id]["inputs"]["lora_name"], item["name"])
            self.assertEqual(graph[node_id]["inputs"]["strength_model"], item["weight"])
        self.assertEqual(graph["71"]["inputs"]["model"], ["krea_identity_extra_lora_10", 0])
        with self.assertRaises(HTTPException):
            self.settings(loras=[*loras, loras[0]])

    def test_identity_parameter_boundaries_are_enforced(self) -> None:
        for changes in (
            {"size_mode": "custom", "width": 1920, "height": 1080},
            {"size_mode": "custom", "width": 2048, "height": 1024},
            {"output_megapixels": 2.0},
        ):
            with self.subTest(valid_changes=changes):
                self.settings(**changes)
        for changes in (
            {"reference_mode": "unknown"},
            {"width": 1025},
            {"width": 2048, "height": 1032},
            {"output_megapixels": 0.24},
            {"output_megapixels": 2.01},
            {"ref_boost": 10.01},
            {"scene_ref_boost": 10.01},
            {"grounding_px": 700},
            {"identity_lora_strength": 2.01},
            {"fit_mode": "stretch"},
        ):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                self.settings(**changes)

    def test_prompt_assistant_writes_identity_restage_instructions(self) -> None:
        values = app_module.normalize_prompt_tool_values({
            "operation": "enhance",
            "target_workflow": "krea-identity-edit",
            "style": "detailed",
            "input": "keep this person's identity and change the scene",
        })
        instruction = app_module.prompt_tool_user_instruction(values)
        self.assertIn("Krea2 图生图", instruction)
        self.assertIn("keep this person's identity", instruction)

    def test_desktop_workflow_uses_official_dual_reference_graph(self) -> None:
        source = ROOT / "workflow_sources" / "Krea2_Identity_Edit.json"
        graph = json.loads(source.read_text(encoding="utf-8"))
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes[55]["widgets_values"][0], app_module.KREA_MODELS["official_turbo"])
        self.assertEqual(nodes[71]["widgets_values"], [app_module.KREA_IDENTITY_EDIT_LORA, 1.0])
        self.assertNotIn(114, nodes)
        self.assertEqual(nodes[53]["inputs"][1]["link"], 22)
        self.assertNotIn(90, nodes)
        self.assertNotIn(92, nodes)
        self.assertIn(115, nodes)
        self.assertIn(116, nodes)
        patch_inputs = {item["name"]: item.get("link") for item in nodes[79]["inputs"]}
        self.assertEqual(patch_inputs["source_latent_b"], 34)
        self.assertEqual(patch_inputs["source_image_b"], 35)
        self.assertEqual(patch_inputs["target_latent"], 38)
        self.assertEqual(nodes[79]["widgets_values"], [4.0, 1.0, "fit"])


if __name__ == "__main__":
    unittest.main()
