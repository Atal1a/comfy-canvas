from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image

import mobile_server.app as app_module


class ThumbnailPreviewTests(unittest.TestCase):
    def test_128_pixel_mobile_preview_is_generated(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            Image.new("RGB", (800, 1200), "navy").save(source)
            with patch.object(app_module, "THUMBNAIL_DIR", root / "thumbnails"):
                preview_path = app_module.thumbnail_for(source, 128)

            self.assertTrue(preview_path.is_file())
            with Image.open(preview_path) as preview:
                self.assertEqual(preview.format, "WEBP")
                self.assertEqual(preview.width, 128)
                self.assertLessEqual(preview.height, 128 * 4)

    def test_192_pixel_preview_is_generated_and_cached(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            Image.new("RGB", (800, 1200), "navy").save(source)
            with patch.object(app_module, "THUMBNAIL_DIR", root / "thumbnails"):
                first = app_module.thumbnail_for(source, 192)
                second = app_module.thumbnail_for(source, 192)

            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            with Image.open(first) as preview:
                self.assertEqual(preview.format, "WEBP")
                self.assertEqual(preview.width, 192)
                self.assertLessEqual(preview.height, 192 * 4)

    def test_unsupported_preview_size_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.png"
            Image.new("RGB", (64, 64), "black").save(source)
            with self.assertRaises(HTTPException) as raised:
                app_module.thumbnail_for(source, 191)

        self.assertEqual(raised.exception.status_code, 400)
