from __future__ import annotations

import json
import unittest

from fastapi import HTTPException

import mobile_server.app as app_module


class SeedModeTests(unittest.TestCase):
    def normalized(self, workflow: str, **updates):
        spec = app_module.get_spec(workflow)
        values = dict(app_module.workflow_descriptor(spec)["defaults"])
        values.update(updates)
        return spec, app_module.normalize_settings(spec, json.dumps(values))

    def test_batch_modes_generate_exact_sequences(self) -> None:
        for mode, expected in (
            ("fixed", [50, 50, 50]),
            ("increment", [50, 51, 52]),
            ("decrement", [50, 49, 48]),
        ):
            spec, values = self.normalized(
                "qwen2511-modular-flux2", count=3, stage1_seed="50",
                stage1_random_seed=False, seed_mode=mode,
            )
            self.assertEqual(app_module.generation_seed_sequence(spec, values), expected)
            self.assertFalse(values["stage1_random_seed"])

    def test_image_edit_sequences_only_change_stage_one_seed(self) -> None:
        spec, values = self.normalized(
            "qwen2511-modular-flux2", count=3, stage1_seed="9007199254740993",
            stage1_random_seed=False, seed_mode="increment",
        )
        self.assertEqual(
            app_module.generation_seed_sequence(spec, values),
            [9007199254740993, 9007199254740994, 9007199254740995],
        )
        self.assertNotIn("stage2_seed", values)

    def test_legacy_random_flags_map_to_seed_modes(self) -> None:
        _, random_values = self.normalized("qwen2511-modular-flux2", stage1_random_seed=True)
        _, fixed_values = self.normalized("qwen2511-modular-flux2", stage1_random_seed=False)
        self.assertEqual(random_values["seed_mode"], "random")
        self.assertEqual(fixed_values["seed_mode"], "fixed")

    def test_seed_sequences_reject_underflow_and_overflow(self) -> None:
        with self.assertRaises(HTTPException):
            self.normalized("qwen2511-modular-flux2", count=2, stage1_seed=str(app_module.MAX_GENERATION_SEED), seed_mode="increment")
        with self.assertRaises(HTTPException):
            self.normalized("qwen2511-modular-flux2", count=2, stage1_seed="0", seed_mode="decrement")


if __name__ == "__main__":
    unittest.main()
