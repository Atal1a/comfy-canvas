from __future__ import annotations

from pathlib import Path
import gc
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mobile_server import app as app_module
from mobile_server import launcher


class LanHttpTests(unittest.TestCase):
    def test_recycle_bin_deep_link_and_refresh_serve_the_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(app_module, "DATABASE", Path(temporary) / "jobs.sqlite3"):
                app_module.init_database()
                app_module.create_user_account("recycle-user", "local-password-123")
                client = TestClient(app_module.app, base_url="http://192.168.1.20:8090")
                try:
                    login = client.post("/api/auth/login", json={
                        "username": "recycle-user", "password": "local-password-123",
                    })
                    self.assertEqual(login.status_code, 200)
                    for _ in range(2):
                        response = client.get("/history/trash", follow_redirects=False)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("text/html", response.headers["content-type"])
                        self.assertIn('id="view-recycle-bin"', response.text)
                finally:
                    client.close()
                    gc.collect()

    def test_login_cookie_works_over_lan_http_and_logout_revokes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(app_module, "DATABASE", Path(temporary) / "jobs.sqlite3"):
                app_module.init_database()
                app_module.create_user_account("lan-user", "local-password-123")
                client = TestClient(app_module.app, base_url="http://192.168.1.20:8090")
                try:
                    login = client.post("/api/auth/login", json={
                        "username": "lan-user", "password": "local-password-123",
                    })
                    self.assertEqual(login.status_code, 200)
                    cookie = login.headers["set-cookie"].lower()
                    self.assertNotIn("; secure", cookie)
                    self.assertIn("httponly", cookie)
                    self.assertIn("samesite=lax", cookie)
                    self.assertEqual(client.get("/api/auth/me").status_code, 200)
                    self.assertEqual(client.get("/api/admin/ca-certificate").status_code, 404)
                    rejected = client.post("/api/auth/logout", headers={"Origin": "http://other.invalid"})
                    self.assertEqual(rejected.status_code, 403)
                    self.assertEqual(client.post("/api/auth/logout").status_code, 200)
                    self.assertEqual(client.get("/api/auth/me").status_code, 401)
                finally:
                    client.close()
                    gc.collect()

    def test_launcher_uses_http_without_proxy_or_certificate_setup(self) -> None:
        with (
            patch.object(launcher, "prepare_environment", return_value={}),
            patch.object(launcher, "port_is_open", return_value=False),
            patch.object(launcher, "ensure_administrator"),
            patch.object(launcher, "start_comfyui"),
            patch.object(launcher, "local_addresses", return_value=["127.0.0.1"]),
            patch.object(launcher.uvicorn, "run") as run_server,
        ):
            launcher.run()
        self.assertFalse(run_server.call_args.kwargs["proxy_headers"])
        self.assertNotIn("ssl_certfile", run_server.call_args.kwargs)
        self.assertNotIn("ssl_keyfile", run_server.call_args.kwargs)

    def test_loopback_bind_does_not_advertise_lan_access(self) -> None:
        self.assertEqual(launcher.local_addresses("127.0.0.1"), ["127.0.0.1"])
