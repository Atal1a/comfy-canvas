from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import mobile_server.app as app_module


class NavigationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_database = app_module.DATABASE
        app_module.DATABASE = Path(self.temporary.name) / "navigation.sqlite3"
        app_module.init_database()

    def tearDown(self) -> None:
        app_module.DATABASE = self.original_database
        gc.collect()
        self.temporary.cleanup()

    def insert_group(self, group_id: str, workflow: str, status: str, submitted_at: str) -> None:
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,storage_scope) VALUES(?,?,?,?,?,'mobile')",
                (group_id, workflow, f"prompt {group_id}", status, submitted_at),
            )

    def insert_job(self, prompt_id: str, group_id: str, status: str) -> None:
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,storage_scope,group_id) VALUES(?,?,?,?,?,'mobile',?)",
                (prompt_id, "qwen2511-modular-flux2", "prompt", status, "2026-01-01T00:00:00+00:00", group_id),
            )

    def test_gallery_supports_workflow_filter_and_pagination(self) -> None:
        self.insert_group("old-qwen", "qwen2511-modular-flux2", "completed", "2026-01-01T00:00:00+00:00")
        self.insert_group("new-krea", "krea-identity-edit", "completed", "2026-01-03T00:00:00+00:00")
        self.insert_group("new-qwen", "qwen2511-modular-flux2", "completed", "2026-01-02T00:00:00+00:00")

        page = asyncio.run(app_module.gallery(limit=2, offset=0))
        self.assertEqual([item["id"] for item in page], ["new-krea", "new-qwen"])
        second_page = asyncio.run(app_module.gallery(limit=2, offset=2))
        self.assertEqual([item["id"] for item in second_page], ["old-qwen"])
        qwen = asyncio.run(app_module.gallery(workflow_key="qwen2511-modular-flux2"))
        self.assertEqual([item["id"] for item in qwen], ["new-qwen", "old-qwen"])

    def test_gallery_uses_id_as_stable_tie_breaker_across_pages(self) -> None:
        submitted_at = "2026-01-03T00:00:00+00:00"
        for group_id in ("same-a", "same-b", "same-c"):
            self.insert_group(group_id, "qwen2511-modular-flux2", "completed", submitted_at)

        newest_first = asyncio.run(app_module.gallery(limit=2, offset=0, sort="newest"))
        newest_second = asyncio.run(app_module.gallery(limit=2, offset=2, sort="newest"))
        oldest = asyncio.run(app_module.gallery(sort="oldest"))

        self.assertEqual([item["id"] for item in newest_first], ["same-c", "same-b"])
        self.assertEqual([item["id"] for item in newest_second], ["same-a"])
        self.assertEqual([item["id"] for item in oldest], ["same-a", "same-b", "same-c"])

    def test_gallery_only_includes_active_image_groups_when_requested(self) -> None:
        self.insert_group("active-image", "qwen2511-modular-flux2", "queued", "2026-01-03T00:00:00+00:00")
        self.insert_group("active-video", "minimax-h3", "running", "2026-01-02T00:00:00+00:00")
        self.insert_group("failed-empty", "qwen2511-modular-flux2", "failed", "2026-01-01T00:00:00+00:00")

        default_gallery = asyncio.run(app_module.gallery())
        active_gallery = asyncio.run(app_module.gallery(include_active=True))

        self.assertEqual(default_gallery, [])
        self.assertEqual([item["id"] for item in active_gallery], ["active-image"])
        self.assertEqual(active_gallery[0]["status"], "queued")
        self.assertEqual(active_gallery[0]["items"], [])

    def test_active_group_progress_uses_configured_generation_count(self) -> None:
        self.insert_group("active-four", "qwen2511-modular-flux2", "running", "2026-01-03T00:00:00+00:00")
        self.insert_job("active-child", "active-four", "running")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "UPDATE generation_groups SET parameters_json=? WHERE group_id='active-four'",
                (json.dumps({"count": 4, "generation_seeds": [1, 2, 3, 4]}),),
            )
        app_module.COMFY_PROGRESS["active-child"] = {
            "percent": 50,
            "approximate": False,
            "label": "正在采样",
        }

        try:
            group = app_module.public_group(app_module.db_group("active-four") or {})
        finally:
            app_module.COMFY_PROGRESS.pop("active-child", None)

        self.assertEqual(group["progress"]["value"], 12)
        self.assertIn("第 1/4 张", group["progress"]["label"])

    def test_gallery_context_centers_anchor_and_reports_more(self) -> None:
        for index in range(7):
            self.insert_group(f"context-{index}", "qwen2511-modular-flux2", "completed", f"2026-01-{index + 1:02d}T00:00:00+00:00")

        context = asyncio.run(app_module.gallery_context("context-3", radius=2, sort="oldest"))

        self.assertEqual([item["id"] for item in context["jobs"]], ["context-1", "context-2", "context-3", "context-4", "context-5"])
        self.assertEqual(context["anchor_index"], 2)
        self.assertTrue(context["has_before"])
        self.assertTrue(context["has_after"])

    def test_gallery_context_rejects_missing_anchor_and_invalid_radius(self) -> None:
        self.insert_group("context-only", "qwen2511-modular-flux2", "completed", "2026-01-01T00:00:00+00:00")
        with self.assertRaises(HTTPException) as missing:
            asyncio.run(app_module.gallery_context("missing"))
        self.assertEqual(missing.exception.status_code, 404)
        with self.assertRaises(HTTPException) as radius:
            asyncio.run(app_module.gallery_context("context-only", radius=51))
        self.assertEqual(radius.exception.status_code, 400)

    def test_gallery_page_and_context_use_bounded_database_connections(self) -> None:
        for index in range(120):
            self.insert_group(
                f"bounded-{index:03d}",
                "qwen2511-modular-flux2",
                "completed",
                f"2026-02-{index + 1:03d}T00:00:00+00:00",
            )

        original_connect = sqlite3.connect
        connection_count = 0

        def counted_connect(*args, **kwargs):
            nonlocal connection_count
            connection_count += 1
            return original_connect(*args, **kwargs)

        with patch.object(app_module.sqlite3, "connect", side_effect=counted_connect):
            page = asyncio.run(app_module.gallery(limit=24))
            page_connections = connection_count
            connection_count = 0
            context = asyncio.run(app_module.gallery_context("bounded-060", radius=24, sort="oldest"))
            context_connections = connection_count

        self.assertEqual(len(page), 24)
        self.assertLessEqual(page_connections, 4)
        self.assertEqual(len(context["jobs"]), 49)
        self.assertLessEqual(context_connections, 4)

    def test_gallery_includes_cancelled_groups_only_when_they_have_visible_results(self) -> None:
        self.insert_group("cancelled-with-result", "krea-identity-edit", "cancelled", "2026-01-03T00:00:00+00:00")
        self.insert_group("cancelled-empty", "krea-identity-edit", "cancelled", "2026-01-02T00:00:00+00:00")
        self.insert_job("cancelled-child", "cancelled-with-result", "completed")
        self.insert_job("cancelled-legacy", None, "cancelled")
        self.insert_job("cancelled-legacy-empty", None, "cancelled")
        image = {"filename": "partial.png", "subfolder": "mobile/krea-identity-edit", "type": "output"}
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "UPDATE jobs SET outputs_json=? WHERE prompt_id='cancelled-child'",
                (json.dumps({"final": [image], "stage1": []}),),
            )
            db.execute(
                "UPDATE jobs SET outputs_json=? WHERE prompt_id='cancelled-legacy'",
                (json.dumps({"final": [image], "stage1": []}),),
            )

        gallery = asyncio.run(app_module.gallery())

        self.assertEqual([item["id"] for item in gallery], ["cancelled-with-result", "cancelled-legacy"])
        self.assertEqual(gallery[0]["status"], "cancelled")
        self.assertEqual(gallery[0]["items"][0]["final"], image)

    def test_gallery_rejects_invalid_ranges(self) -> None:
        with self.assertRaises(HTTPException) as limit:
            asyncio.run(app_module.gallery(limit=101))
        self.assertEqual(limit.exception.status_code, 400)
        with self.assertRaises(HTTPException) as offset:
            asyncio.run(app_module.gallery(offset=-1))
        self.assertEqual(offset.exception.status_code, 400)

    def test_public_parameters_preserve_large_seed_digits(self) -> None:
        seed = 8_396_115_963_510_512_347
        parameters = app_module.public_parameters({"parameters_json": json.dumps({
            "seed": seed,
            "stage1_seed": seed - 1,
            "stage2_seed": seed - 2,
            "stage1_seeds": [seed],
            "generation_seeds": [seed - 3],
            "stage1_steps": 8,
        })})
        self.assertEqual(parameters["seed"], str(seed))
        self.assertEqual(parameters["stage1_seed"], str(seed - 1))
        self.assertEqual(parameters["stage2_seed"], str(seed - 2))
        self.assertEqual(parameters["stage1_seeds"], [str(seed)])
        self.assertEqual(parameters["generation_seeds"], [str(seed - 3)])
        self.assertEqual(parameters["stage1_steps"], 8)

    def test_seed_string_normalization_keeps_all_digits(self) -> None:
        maximum = "18446744073709551615"
        self.assertEqual(app_module.as_number(maximum, "seed", 0, 2**64 - 1, True), 2**64 - 1)

    def test_public_group_preserves_prompt_before_enhancement(self) -> None:
        self.insert_group("enhanced", "krea-identity-edit", "completed", "2026-01-03T00:00:00+00:00")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "UPDATE generation_groups SET source_prompt_text=? WHERE group_id=?",
                ("原始简短提示词", "enhanced"),
            )

        result = app_module.public_group(app_module.db_group("enhanced"))

        self.assertEqual(result["source_prompt"], "原始简短提示词")

    def test_running_group_exposes_each_completed_child_immediately(self) -> None:
        self.insert_group("progressive", "krea-identity-edit", "running", "2026-01-03T00:00:00+00:00")
        self.insert_job("finished-child", "progressive", "completed")
        self.insert_job("running-child", "progressive", "running")
        image = {"filename": "first.png", "subfolder": "krea-identity-edit", "type": "output"}
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "UPDATE jobs SET outputs_json=? WHERE prompt_id='finished-child'",
                (json.dumps({"final": [image], "stage1": []}),),
            )

        result = app_module.public_group(app_module.db_group("progressive"))

        self.assertEqual(result["status"], "running")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["final"], image)

    def test_completed_managed_detail_does_not_touch_scheduler_or_comfy(self) -> None:
        self.insert_group("managed-done", "krea-identity-edit", "completed", "2026-01-03T00:00:00+00:00")
        self.insert_job("managed-prompt", "managed-done", "completed")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO users(username,username_key,password_hash,role,created_at,updated_at) "
                "VALUES('tester','tester','hash','admin',?,?)",
                ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            db.execute(
                "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at) "
                "VALUES('generation','managed-done',1,'completed',?)",
                ("2026-01-03T00:00:00+00:00",),
            )

        with (
            patch.object(app_module, "scheduler_tick", AsyncMock()) as scheduler,
            patch.object(app_module, "comfy_json", AsyncMock()) as comfy,
            patch.object(app_module, "recover_stuck_groups") as recovery,
        ):
            result = asyncio.run(app_module.job("managed-done"))

        self.assertEqual(result["status"], "completed")
        scheduler.assert_not_awaited()
        comfy.assert_not_awaited()
        recovery.assert_not_called()

    def test_public_group_bulk_loads_related_records_and_indexes_exist(self) -> None:
        self.insert_group("related", "krea-identity-edit", "completed", "2026-01-03T00:00:00+00:00")
        item = {
            "id": "item-1",
            "prompt_id": "related-prompt",
            "final": {"filename": "missing.png", "subfolder": "", "type": "output"},
            "stage1": None,
        }
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "UPDATE generation_groups SET items_json=? WHERE group_id='related'",
                (json.dumps([item]),),
            )
            db.execute(
                "INSERT INTO upscale_jobs(upscale_id,parent_group_id,parent_item_id,status,submitted_at) "
                "VALUES('upscale-1','related','item-1','failed',?)",
                ("2026-01-03T00:01:00+00:00",),
            )
            db.execute(
                "INSERT INTO detail_jobs(detail_id,parent_group_id,parent_item_id,status,submitted_at) "
                "VALUES('detail-1','related','item-1','failed',?)",
                ("2026-01-03T00:02:00+00:00",),
            )
            index_names = {
                row[1]
                for table in ("jobs", "upscale_jobs", "detail_jobs")
                for row in db.execute(f"PRAGMA index_list({table})")
            }

        with (
            patch.object(app_module, "db_item_upscales", side_effect=AssertionError("N+1 upscale query")),
            patch.object(app_module, "db_item_details", side_effect=AssertionError("N+1 detail query")),
        ):
            result = app_module.public_group(app_module.db_group("related"))

        self.assertEqual([record["id"] for record in result["items"][0]["upscales"]], ["upscale-1"])
        self.assertEqual([record["id"] for record in result["items"][0]["details"]], ["detail-1"])
        self.assertTrue({
            "idx_jobs_group_active_order",
            "idx_upscale_jobs_group_item_time",
            "idx_detail_jobs_group_item_time",
        }.issubset(index_names))

    def test_activity_lists_active_mobile_groups_newest_first(self) -> None:
        self.insert_group("queued-old", "qwen2511-modular-flux2", "queued", "2026-01-01T00:00:00+00:00")
        self.insert_group("running-new", "qwen2511-modular-flux2", "running", "2026-01-02T00:00:00+00:00")
        self.insert_group("complete", "qwen2511-modular-flux2", "completed", "2026-01-03T00:00:00+00:00")

        items = asyncio.run(app_module.activity())
        self.assertEqual([item["id"] for item in items], ["running-new", "queued-old"])

    def test_all_application_routes_are_registered(self) -> None:
        paths = {route.path for route in app_module.app.routes}
        self.assertTrue({
            "/", "/create", "/flux-upscale", "/history", "/jobs/{prompt_id}", "/settings",
            "/settings/templates", "/settings/tips", "/settings/collections", "/settings/storage", "/settings/admin",
        }.issubset(paths))

    def test_cancel_group_deletes_queued_and_interrupts_running_prompts(self) -> None:
        self.insert_group("active", "qwen2511-modular-flux2", "running", "2026-01-01T00:00:00+00:00")
        self.insert_job("running-prompt", "active", "running")
        self.insert_job("queued-prompt", "active", "queued")
        comfy = AsyncMock(side_effect=[
            {"queue_running": [[1, "running-prompt", {}, {}, []]], "queue_pending": [[2, "queued-prompt", {}, {}, []]]},
            {},
            {},
            {},
            {},
        ])

        with patch.object(app_module, "comfy_json", comfy):
            result = asyncio.run(app_module.cancel_job("active"))

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual([row["status"] for row in app_module.db_group_jobs("active")], ["cancelled", "cancelled"])
        self.assertEqual(comfy.await_args_list[1].args, ("POST", "/queue"))
        self.assertEqual(comfy.await_args_list[1].kwargs["json"], {"delete": ["queued-prompt"]})
        self.assertEqual(comfy.await_args_list[2].args, ("POST", "/interrupt"))

    def test_cancel_group_rejects_terminal_task(self) -> None:
        self.insert_group("done", "qwen2511-modular-flux2", "completed", "2026-01-01T00:00:00+00:00")
        self.insert_job("done-prompt", "done", "completed")

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(app_module.cancel_job("done"))

        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
