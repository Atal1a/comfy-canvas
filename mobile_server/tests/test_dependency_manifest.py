from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from mobile_server.dependency_manifest import (
    DependencyManifestError,
    load_dependency_manifest,
    select_dependencies,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DependencyManifestTests(unittest.TestCase):
    def test_public_manifest_is_valid_and_main_profile_is_minimal(self) -> None:
        dependencies = load_dependency_manifest(PROJECT_ROOT)
        main = select_dependencies(dependencies, "main")

        self.assertEqual(len(dependencies), 11)
        samplers = next(item for item in dependencies if item.name == "RES4LYF")
        self.assertEqual(samplers.required_by, ("enhanced-upscale",))
        cleanup = next(item for item in dependencies if item.name == "comfyui_memory_cleanup")
        self.assertEqual(cleanup.required_by, ("enhanced-upscale",))
        self.assertEqual(cleanup.install_profile, "optional")
        self.assertEqual(
            {item.name for item in main},
            {"comfyui-krea2edit", "ComfyUI-KJNodes", "ComfyUI-MiniMax-H3-Turbo"},
        )
        self.assertTrue(all(item.license_spdx for item in dependencies))

    def test_manifest_rejects_moving_commit_reference(self) -> None:
        source = json.loads((PROJECT_ROOT / "manifests/dependencies.lock.json").read_text(encoding="utf-8"))
        source["verified_public_custom_nodes"][0]["commit"] = "main"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifests").mkdir()
            (root / "patches").mkdir()
            (root / "patches" / "comfyui-progress.patch").write_text("patch", encoding="utf-8")
            (root / "manifests/dependencies.lock.json").write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(DependencyManifestError, "40-character"):
                load_dependency_manifest(root)

    def test_unknown_install_profile_is_rejected(self) -> None:
        dependencies = load_dependency_manifest(PROJECT_ROOT)
        with self.assertRaisesRegex(DependencyManifestError, "unknown install profile"):
            select_dependencies(dependencies, "everything")


if __name__ == "__main__":
    unittest.main()
