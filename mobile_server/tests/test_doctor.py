from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from mobile_server.config import load_settings
from mobile_server.doctor import collect_checks


class DoctorTests(unittest.TestCase):
    def test_source_only_check_validates_public_workflows_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text("", encoding="utf-8")
            checks = collect_checks(load_settings(config), source_only=True)

        self.assertEqual(
            {check.name for check in checks},
            {"python", "workflows", "config", "dependencies", "model-manifest"},
        )
        self.assertFalse(any(check.level == "error" for check in checks))


if __name__ == "__main__":
    unittest.main()
