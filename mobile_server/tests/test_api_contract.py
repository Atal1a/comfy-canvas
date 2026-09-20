from __future__ import annotations

import unittest

from mobile_server.app import app


CORE_API_CONTRACT = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/register"),
    ("GET", "/api/auth/me"),
    ("PATCH", "/api/account/profile"),
    ("PUT", "/api/account/avatar"),
    ("DELETE", "/api/account/avatar"),
    ("POST", "/api/account/password"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/logout-all"),
    ("GET", "/api/account/storage"),
    ("GET", "/api/queue"),
    ("POST", "/api/queue/{queue_id}/cancel"),
    ("GET", "/api/admin/queue"),
    ("POST", "/api/admin/comfy-recovery"),
    ("PUT", "/api/admin/performance-mode"),
    ("POST", "/api/admin/queue/{queue_id}/run-next"),
    ("GET", "/api/admin/activity"),
    ("POST", "/api/admin/activity/{kind}/{record_id}/cancel"),
    ("GET", "/api/workflows"),
    ("POST", "/api/workflows/minimax-h3/estimate"),
    ("GET", "/api/gallery"),
    ("GET", "/api/gallery/context"),
    ("PUT", "/api/lora-favorites/{family}/{lora_name:path}"),
    ("DELETE", "/api/lora-favorites/{family}/{lora_name:path}"),
    ("GET", "/api/admin/lora-metadata"),
    ("GET", "/api/admin/lora-categories"),
    ("POST", "/api/admin/lora-categories"),
    ("PATCH", "/api/admin/lora-categories/{category_id}"),
    ("DELETE", "/api/admin/lora-categories/{category_id}"),
    ("GET", "/api/admin/lora-scan"),
    ("PATCH", "/api/admin/lora-files/{family}/{lora_name:path}"),
    ("PUT", "/api/admin/lora-metadata/{family}/{lora_name:path}"),
    ("DELETE", "/api/admin/lora-metadata/{family}/{lora_name:path}"),
    ("DELETE", "/api/workflows/{workflow_key}/outputs"),
}


class CoreApiContractTests(unittest.TestCase):
    def test_core_domain_routes_are_registered_once(self) -> None:
        registered = [
            (method, route.path)
            for route in app.routes
            for method in sorted(getattr(route, "methods", set()) or set())
        ]
        for endpoint in CORE_API_CONTRACT:
            self.assertEqual(registered.count(endpoint), 1, endpoint)


if __name__ == "__main__":
    unittest.main()
