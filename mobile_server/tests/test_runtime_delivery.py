from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.responses import Response
import httpx

from mobile_server import backup
from mobile_server import app as app_module
from mobile_server.runtime import (
    PrecompressedStaticFiles,
    apply_security_headers,
    precompressed_file_response,
)


def request_with_headers(*headers: tuple[bytes, bytes]) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": "/static/app.js",
        "raw_path": b"/static/app.js",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 443),
    })


class RuntimeDeliveryTests(unittest.TestCase):
    def test_precompressed_file_prefers_brotli_and_keeps_original_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "app.js"
            source.write_bytes(b"source")
            Path(f"{source}.br").write_bytes(b"brotli")
            response = precompressed_file_response(
                request_with_headers((b"accept-encoding", b"gzip, br")),
                source,
            )

            self.assertEqual(Path(response.path), Path(f"{source}.br"))
            self.assertEqual(response.headers["content-encoding"], "br")
            self.assertIn("javascript", response.headers["content-type"])

    def test_static_files_serves_gzip_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "style.css"
            source.write_bytes(b"source")
            Path(f"{source}.gz").write_bytes(b"gzip")
            files = PrecompressedStaticFiles(directory=temporary)
            request = request_with_headers((b"accept-encoding", b"gzip"))

            response = asyncio.run(files.get_response("style.css", request.scope))

            self.assertEqual(response.headers["content-encoding"], "gzip")
            self.assertTrue(response.headers["content-type"].startswith("text/css"))

    def test_security_headers_block_embedding_and_external_scripts(self) -> None:
        response = Response()
        apply_security_headers(response)

        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])

    def test_app_serves_precompressed_hashed_assets_with_security_and_cache_headers(self) -> None:
        asset = next(
            path for path in (app_module.STATIC_DIR / "assets").glob("app-*.js")
            if path.suffix == ".js"
        )

        async def request_asset() -> httpx.Response:
            transport = httpx.ASGITransport(app=app_module.app)
            async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
                return await client.get(
                    f"/static/assets/{asset.name}",
                    headers={"Accept-Encoding": "br"},
                )

        response = asyncio.run(request_asset())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "br")
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_runtime_backup_is_consistent_and_includes_avatars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "jobs.sqlite3"
            avatars = root / "avatars"
            avatars.mkdir()
            (avatars / "1.webp").write_bytes(b"avatar")
            with closing(sqlite3.connect(database)) as db:
                db.execute("CREATE TABLE sample(value TEXT)")
                db.execute("INSERT INTO sample(value) VALUES('kept')")
                db.commit()

            with (
                patch.object(backup, "DATABASE", database),
                patch.object(backup, "AVATAR_DIR", avatars),
                patch.object(backup, "CHAT_MEDIA_DIR", root / "chat_media"),
            ):
                output = backup.create_backup(root / "backups")

            with closing(sqlite3.connect(output / "jobs.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT value FROM sample").fetchone()[0], "kept")
            self.assertEqual((output / "avatars" / "1.webp").read_bytes(), b"avatar")
            self.assertTrue((output / "manifest.json").is_file())

    def test_runtime_diagnostics_groups_queue_rows_by_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "jobs.sqlite3"
            with (
                patch.object(app_module, "DATABASE", database),
                patch.object(app_module, "MOBILE_OUTPUT_DIR", root / "output"),
                patch.object(app_module, "THUMBNAIL_DIR", root / "thumbnails"),
                patch.object(app_module, "CHAT_MEDIA_DIR", root / "chat_media"),
                patch.object(app_module, "AVATAR_DIR", root / "avatars"),
                patch.object(app_module, "DATA_DIR", root / "data"),
                patch.object(app_module, "STATIC_DIR", root / "static_dist"),
            ):
                app_module.init_database()
                with closing(sqlite3.connect(database)) as db:
                    db.execute(
                        "INSERT INTO users(username,username_key,password_hash,role,created_at,updated_at) "
                        "VALUES('admin','admin','hash','admin','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
                    )
                    db.execute(
                        "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at) "
                        "VALUES('generation','record-1',1,'waiting','2026-01-01T00:00:00+00:00')"
                    )
                    db.commit()

                snapshot = app_module.runtime_diagnostics_snapshot()

            self.assertEqual(snapshot["database"]["quick_check"], "ok")
            self.assertEqual(snapshot["queue"], {"waiting": 1})

    def test_runtime_diagnostics_cache_reuses_snapshot_until_invalidated(self) -> None:
        snapshot = {
            "generated_at": "2026-08-30T10:00:00+00:00",
            "database": {"quick_check": "ok", "files": {}, "rows": {}},
            "queue": {}, "storage": {}, "frontend": {},
        }
        app_module.invalidate_diagnostics_cache()
        with patch.object(app_module, "runtime_diagnostics_snapshot", return_value=snapshot) as loader:
            first = app_module.cached_runtime_diagnostics_snapshot()
            second = app_module.cached_runtime_diagnostics_snapshot()
            app_module.invalidate_diagnostics_cache()
            third = app_module.cached_runtime_diagnostics_snapshot()

        self.assertEqual(loader.call_count, 2)
        self.assertFalse(first["stale"])
        self.assertEqual(second["generated_at"], first["generated_at"])
        self.assertEqual(third["database"]["quick_check"], "ok")

    def test_schema_baseline_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "jobs.sqlite3"
            with patch.object(app_module, "DATABASE", database):
                app_module.init_database()
                with closing(sqlite3.connect(database)) as db:
                    row = db.execute(
                        "SELECT version,name FROM schema_migrations ORDER BY version DESC LIMIT 1"
                    ).fetchone()

        self.assertEqual(row, (1, "portable-schema-baseline"))


if __name__ == "__main__":
    unittest.main()
