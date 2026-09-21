import json
from pathlib import Path
import unittest

from mobile_server import __version__
from mobile_server.app import app


class ReleaseVersionTests(unittest.TestCase):
    def test_release_metadata_uses_one_version(self):
        root = Path(__file__).resolve().parents[2]
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(package["version"], __version__)
        self.assertEqual(lock["packages"][""]["version"], __version__)
        self.assertEqual(app.version, __version__)
        self.assertEqual(app.openapi()["info"]["version"], __version__)
        self.assertIn(f"## [{__version__}]", (root / "docs/CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertTrue((root / "docs" / "releases" / f"{__version__}.md").is_file())
        self.assertEqual(package["license"], "GPL-3.0-only")
        self.assertEqual(lock["packages"][""]["license"], package["license"])


if __name__ == "__main__":
    unittest.main()
