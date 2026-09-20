import tempfile
import unittest
from pathlib import Path

from mobile_server.model_paths import model_path


class ModelPathTests(unittest.TestCase):
    def test_legacy_directories_and_nested_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for current, legacy in (("text_encoders", "clip"), ("diffusion_models", "unet")):
                path = root / "models" / legacy / "author" / "model.safetensors"
                path.parent.mkdir(parents=True)
                path.touch()
                self.assertEqual(model_path(root, current, "author/model.safetensors"), path)

    def test_current_directory_takes_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for folder in ("text_encoders", "clip"):
                path = root / "models" / folder / "encoder.safetensors"
                path.parent.mkdir(parents=True)
                path.touch()
            self.assertEqual(model_path(root, "text_encoders", "encoder.safetensors"),
                             root / "models/text_encoders/encoder.safetensors")

    def test_missing_file_is_not_replaced_with_another_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = root / "models/clip/other.safetensors"
            other.parent.mkdir(parents=True)
            other.touch()
            self.assertFalse(model_path(root, "text_encoders", "missing.safetensors").is_file())

    def test_diffusion_precedence_matches_comfyui(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for folder in ("unet", "diffusion_models"):
                path = root / "models" / folder / "model.safetensors"
                path.parent.mkdir(parents=True)
                path.touch()
            self.assertEqual(model_path(root, "diffusion_models", "model.safetensors"),
                             root / "models/unet/model.safetensors")

    def test_unrelated_directories_are_not_searched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = root / "models/clip/vae.safetensors"
            other.parent.mkdir(parents=True)
            other.touch()
            self.assertFalse(model_path(root, "vae", "vae.safetensors").is_file())
