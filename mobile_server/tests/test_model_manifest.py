from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from mobile_server.model_manifest import ModelManifestError, load_model_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ModelManifestTests(unittest.TestCase):
    def test_public_model_manifest_covers_all_generation_defaults(self) -> None:
        models = load_model_manifest(PROJECT_ROOT)

        self.assertEqual(len(models), 12)
        self.assertEqual(len({key for model in models for key in model.config_keys}), 16)
        self.assertTrue(all(model.source_url.startswith("https://huggingface.co/") for model in models))

    def test_manifest_rejects_default_filename_drift(self) -> None:
        document = json.loads((PROJECT_ROOT / "manifests/models.lock.json").read_text(encoding="utf-8"))
        document["models"][0]["filename"] = "renamed.safetensors"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifests").mkdir()
            (root / "manifests/models.lock.json").write_text(json.dumps(document), encoding="utf-8")
            (root / "config.example.toml").write_text(
                (PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ModelManifestError, "does not match"):
                load_model_manifest(root)

    def test_manifest_rejects_invalid_hash(self) -> None:
        document = json.loads((PROJECT_ROOT / "manifests/models.lock.json").read_text(encoding="utf-8"))
        document["models"][0]["sha256"] = "unknown"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifests").mkdir()
            (root / "manifests/models.lock.json").write_text(json.dumps(document), encoding="utf-8")
            (root / "config.example.toml").write_text(
                (PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ModelManifestError, "lowercase SHA-256"):
                load_model_manifest(root)


if __name__ == "__main__":
    unittest.main()
