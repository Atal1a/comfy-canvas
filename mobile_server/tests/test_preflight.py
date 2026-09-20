import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from mobile_server.services.preflight import validate_dependencies


class PreflightTests(unittest.TestCase):
    def test_missing_node_names_the_dependency(self):
        with self.assertRaisesRegex(HTTPException, "VRAMCleanup"):
            validate_dependencies({"1": {"class_type": "VRAMCleanup"}}, {"SaveImage": {}})

    def test_missing_sampler_names_the_value(self):
        graph = {"211": {"class_type": "KSampler", "inputs": {"sampler_name": "res_2s"}}}
        info = {"KSampler": {"input": {"required": {"sampler_name": [["euler"]]}}}}
        with self.assertRaisesRegex(HTTPException, "res_2s"):
            validate_dependencies(graph, info)

    def test_comfyui_options_allow_user_model_names_and_links(self):
        graph = {"1": {"class_type": "Loader", "inputs": {"unet_name": "user.safetensors", "model": ["2", 0]}}}
        info = {"Loader": {"input": {"required": {"unet_name": [["user.safetensors"]], "model": ["MODEL"]}}}}
        validate_dependencies(graph, info)

    def test_unavailable_catalog_is_not_silently_ignored(self):
        with self.assertRaises(HTTPException):
            validate_dependencies({}, {})


class SubmissionPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_dependency_never_posts_prompt(self):
        import mobile_server.app as app
        request = AsyncMock(return_value={"SaveImage": {}})
        with patch.object(app, "comfy_json", request):
            with self.assertRaises(HTTPException):
                await app.post_comfy_prompt({"1": {"class_type": "VRAMCleanup"}}, {})
        request.assert_awaited_once_with("GET", "/object_info")
