from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.package_release import public_showcase_files, source_files


class ReleasePackageTests(unittest.TestCase):
    def test_relocated_english_readme_resolves_showcase_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs/showcase").mkdir(parents=True)
            (root / "README.md").write_text("[English](docs/README.en.md)")
            (root / "docs/README.en.md").write_text("[Home](../README.md) [Film](showcase/current.mp4)")
            (root / "docs/showcase/current.mp4").touch()
            self.assertEqual(public_showcase_files(root), {Path("docs/showcase/current.mp4")})

    def test_relocated_windows_helpers_resolve_project_root(self):
        root = Path(__file__).resolve().parents[2]
        for name in ("启动ComfyUI维护模式.bat", "配置局域网防火墙.bat"):
            script = (root / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn('set "BASE=%~dp0..\\"', script)
            self.assertIn('cd /d "%BASE%"', script)

    def test_showcase_only_includes_referenced_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs/showcase").mkdir(parents=True)
            (root / "README.md").write_text("[Film](docs/showcase/current.mp4)")
            (root / "docs/showcase/current.mp4").touch()
            (root / "docs/showcase/old.mp4").touch()
            self.assertEqual(public_showcase_files(root), {Path("docs/showcase/current.mp4")})

    def test_missing_document_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("[Film](docs/showcase/missing.mp4)")
            with self.assertRaises(ValueError):
                public_showcase_files(root)

    def test_tracked_private_files_are_rejected(self):
        for name in ("config.toml", "data/users.db", "portfolio-plan.md", "model.safetensors"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                with patch("tools.package_release.subprocess.run", return_value=subprocess.CompletedProcess([], 0, (name + "\0").encode())):
                    with self.assertRaises(ValueError):
                        source_files(root)

    def test_deleted_sources_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").touch()
            with patch("tools.package_release.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"README.md\0removed.py\0")):
                self.assertEqual(source_files(root), [Path("README.md")])
