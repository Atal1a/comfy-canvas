import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi import HTTPException

from mobile_server.services.media import private_video_poster_response


class VideoPosterFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_ffmpeg_shows_message_without_caching_failure(self):
        response = await private_video_poster_response(
            Path("video.mp4"), 512,
            create_thumbnail=Mock(side_effect=HTTPException(503, "unavailable")),
        )
        self.assertEqual(response.media_type, "image/svg+xml")
        self.assertIn("FFmpeg", response.body.decode())
        self.assertIn("no-store", response.headers["Cache-Control"])

    async def test_bad_size_is_not_hidden(self):
        with self.assertRaises(HTTPException) as caught:
            await private_video_poster_response(
                Path("video.mp4"), 1,
                create_thumbnail=Mock(side_effect=HTTPException(400, "bad size")),
            )
        self.assertEqual(caught.exception.status_code, 400)

    async def test_valid_poster_remains_webp(self):
        response = await private_video_poster_response(
            Path("video.mp4"), 512, create_thumbnail=Mock(return_value=Path("poster.webp")),
        )
        self.assertEqual(response.media_type, "image/webp")
