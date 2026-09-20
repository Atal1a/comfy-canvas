from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from mobile_server.config import ConfigError, PROJECT_ROOT, load_settings


class ConfigurationTests(unittest.TestCase):
    def write_config(self, content: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_defaults_use_source_distribution_layout(self) -> None:
        settings = load_settings(self.write_config(""))

        self.assertEqual(settings.comfyui_root, PROJECT_ROOT / "runtime" / "ComfyUI")
        self.assertEqual(settings.data_dir, PROJECT_ROOT / "data")
        self.assertEqual(settings.output_dir, PROJECT_ROOT / "data" / "output")
        self.assertFalse(settings.comfy_auto_start)
        self.assertEqual(settings.comfy_url, "http://127.0.0.1:8388")
        self.assertEqual(settings.server_port, 8090)

    def test_paths_limits_and_workflows_are_configurable(self) -> None:
        settings = load_settings(self.write_config(r"""
[paths]
comfyui = "vendor/ComfyUI"
data = "runtime-data"
ffmpeg = "tools/ffmpeg.exe"

[comfyui]
url = "http://localhost:9001"
auto_start = false
launch_args = ["--cpu"]

[server]
host = "127.0.0.1"
port = 9443

[features]
workflows = ["krea-identity-edit"]

[models.krea_identity]
base_model = "models/krea-base.safetensors"
text_encoder = "models/krea-text.safetensors"
vae = "models/krea-vae.safetensors"
identity_lora = "models/krea-identity.safetensors"

[models.enhanced_upscale]
base_model = "models/flux-upscale.safetensors"
text_encoder = "models/flux-text.safetensors"
vae = "models/flux-vae.safetensors"
consistency_lora = 'author\consistency.safetensors'
seedvr_dit = "models/seedvr-dit.safetensors"
seedvr_vae = "models/seedvr-vae.safetensors"

[prompt_assistant]
enhance_instruction = "Preserve the user's requested details."

[limits]
user_quota_mb = 512
image_upload_mb = 8
queued_tasks_per_user = 1
"""))

        self.assertEqual(settings.comfyui_root, PROJECT_ROOT / "vendor" / "ComfyUI")
        self.assertEqual(settings.data_dir, PROJECT_ROOT / "runtime-data")
        self.assertEqual(settings.ffmpeg_path, PROJECT_ROOT / "tools" / "ffmpeg.exe")
        self.assertEqual(settings.comfy_host, "localhost")
        self.assertEqual(settings.comfy_port, 9001)
        self.assertFalse(settings.comfy_auto_start)
        self.assertEqual(settings.comfy_launch_args, ("--cpu",))
        self.assertEqual(settings.enabled_workflows, ("krea-identity-edit",))
        self.assertEqual(settings.krea_identity_model, "models/krea-base.safetensors")
        self.assertEqual(settings.krea_identity_text_encoder, "models/krea-text.safetensors")
        self.assertEqual(settings.krea_identity_vae, "models/krea-vae.safetensors")
        self.assertEqual(settings.krea_identity_lora, "models/krea-identity.safetensors")
        self.assertEqual(settings.enhanced_upscale_model, "models/flux-upscale.safetensors")
        self.assertEqual(settings.enhanced_upscale_text_encoder, "models/flux-text.safetensors")
        self.assertEqual(settings.enhanced_upscale_vae, "models/flux-vae.safetensors")
        self.assertEqual(settings.enhanced_upscale_consistency_lora, r"author\consistency.safetensors")
        self.assertEqual(settings.enhanced_upscale_seedvr_dit, "models/seedvr-dit.safetensors")
        self.assertEqual(settings.enhanced_upscale_seedvr_vae, "models/seedvr-vae.safetensors")
        self.assertEqual(
            settings.prompt_enhance_instruction,
            "Preserve the user's requested details.",
        )
        self.assertEqual(settings.server_port, 9443)
        self.assertEqual(settings.user_quota_bytes, 512 * 1024 * 1024)
        self.assertEqual(settings.max_upload_bytes, 8 * 1024 * 1024)
        self.assertEqual(settings.queue_user_limit, 1)

    def test_unknown_setting_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "server.prot"):
            load_settings(self.write_config("[server]\nprot = 9000\n"))

    def test_invalid_boolean_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must be true or false"):
            load_settings(self.write_config('[comfyui]\nauto_start = "false"\n'))

    def test_invalid_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "http URL without a path"):
            load_settings(self.write_config('[comfyui]\nurl = "https://example.test/comfy"\n'))


if __name__ == "__main__":
    unittest.main()
