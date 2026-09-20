from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import gc
import inspect
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

import mobile_server.app as app_module
import mobile_server.control as control_module


class TaskQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.originals = {name: getattr(app_module, name) for name in ("DATABASE", "COMFY_ROOT", "UPLOAD_DIR", "OUTPUT_DIR", "MOBILE_OUTPUT_DIR", "H3_HEARTBEAT_DIR")}
        app_module.DATABASE = root / "queue.sqlite3"
        app_module.COMFY_ROOT = root / "ComfyUI"
        app_module.UPLOAD_DIR = app_module.COMFY_ROOT / "input" / "mobile_uploads"
        app_module.OUTPUT_DIR = root / "output"
        app_module.MOBILE_OUTPUT_DIR = app_module.OUTPUT_DIR / "mobile"
        app_module.H3_HEARTBEAT_DIR = app_module.COMFY_ROOT / "user" / "h3_heartbeats"
        app_module.UPLOAD_DIR.mkdir(parents=True)
        app_module.MOBILE_OUTPUT_DIR.mkdir(parents=True)
        app_module.H3_HEARTBEAT_DIR.mkdir(parents=True)
        app_module.SCHEDULER_LOCK = None
        app_module.COMFY_PROGRESS.clear()
        app_module.init_database()
        self.admin = app_module.create_admin_account("owner", "administrator-password")
        self.alice = app_module.create_user_account("alice", "alice-secure-password")
        self.bob = app_module.create_user_account("bob", "bob-secure-password")

    def test_database_uses_wal_for_concurrent_readers(self) -> None:
        with app_module.closing(app_module.db_connect()) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def tearDown(self) -> None:
        app_module.CURRENT_USER.set(None)
        app_module.SCHEDULER_LOCK = None
        app_module.COMFY_PROGRESS.clear()
        for name, value in self.originals.items():
            setattr(app_module, name, value)
        gc.collect()
        self.temporary.cleanup()

    def add_group(self, record_id: str, owner_id: int, state: str = "waiting", prompt_id: str | None = None) -> int:
        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,parameters_json,storage_scope,owner_id) VALUES(?,?,?,?,?,?,'mobile',?)",
                (record_id, "qwen2511-modular-flux2", "private prompt", "running" if prompt_id else "queued", now, json.dumps({"count": 1}), owner_id),
            )
            if prompt_id:
                db.execute(
                    "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,group_id,owner_id) VALUES(?,?,?,?,?,?,?)",
                    (prompt_id, "qwen2511-modular-flux2", "private prompt", "running", now, record_id, owner_id),
                )
            cursor = db.execute(
                "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at,started_at,prompt_id,profile_key) VALUES('generation',?,?,?,?,?,?,?)",
                (record_id, owner_id, state, now, now if prompt_id else None, prompt_id, "generation:qwen2511-modular-flux2:count=1"),
            )
        return int(cursor.lastrowid)

    def add_h3_group(self, record_id: str, owner_id: int, parameters: dict | None = None) -> None:
        values = parameters or {
            "negative_prompt": "historical negative", "count": 1, "mode": "t2v", "aspect_ratio": "16:9",
            "megapixels": 0.6, "duration": 5, "seed": 37, "random_seed": False, "steps": 8,
            "h3_model": "original_int8", "turbo_lora": app_module.H3_TURBO_LORAS[0],
            "lora_strength": 1.0, "low_vram": False, "loras": [],
        }
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,parameters_json,storage_scope,owner_id) VALUES(?,?,?,?,?,?,'mobile',?)",
                (record_id, "minimax-h3", "historical prompt", "completed", app_module.utc_now(), json.dumps(values), owner_id),
            )

    def test_shared_queue_is_fifo_and_anonymous(self) -> None:
        first = self.add_group("alice-one", self.alice["user_id"])
        second = self.add_group("bob-one", self.bob["user_id"])
        snapshot = app_module.queue_snapshot(self.alice["user_id"])
        self.assertEqual([item["id"] for item in snapshot["tasks"]], [first, second])
        self.assertEqual(snapshot["tasks"][0]["owner"], "我的任务")
        self.assertEqual(snapshot["tasks"][1]["owner"], "其他用户")
        self.assertNotIn("private prompt", json.dumps(snapshot, ensure_ascii=False))
        self.assertNotIn("bob", json.dumps(snapshot, ensure_ascii=False))

    def test_workflow_progress_is_monotonic_across_comfy_nodes(self) -> None:
        graph = {
            "load": {"class_type": "UNETLoader", "inputs": {}},
            "seed": {"class_type": "SeedVR2VideoUpscaler", "inputs": {}},
            "sample": {"class_type": "KSampler", "inputs": {}},
            "decode": {"class_type": "VAEDecode", "inputs": {}},
            "save": {"class_type": "SaveImage", "inputs": {}},
        }
        app_module.register_comfy_prompt("progress-prompt", graph)
        app_module.update_comfy_progress("progress-prompt", "seed", 10, 10)
        seed_finished = app_module.COMFY_PROGRESS["progress-prompt"]["percent"]
        app_module.update_comfy_progress("progress-prompt", "sample", 0, 8)
        self.assertGreaterEqual(app_module.COMFY_PROGRESS["progress-prompt"]["percent"], seed_finished)
        app_module.update_comfy_progress("progress-prompt", "sample", 4, 8)
        self.assertEqual(app_module.COMFY_PROGRESS["progress-prompt"]["label"], "Flux 正在采样")
        self.assertGreater(app_module.COMFY_PROGRESS["progress-prompt"]["percent"], seed_finished)
        app_module.update_comfy_progress("progress-prompt", "save")
        self.assertLessEqual(app_module.COMFY_PROGRESS["progress-prompt"]["percent"], 99)
        app_module.mark_comfy_progress_complete("progress-prompt")
        self.assertEqual(app_module.COMFY_PROGRESS["progress-prompt"]["percent"], 100)

    def test_enhanced_upscale_exposes_monotonic_stages_and_exact_node_steps(self) -> None:
        graph = {
            "load_image": {"class_type": "LoadImage", "inputs": {}},
            "seed_model": {"class_type": "SeedVR2LoadDiTModel", "inputs": {}},
            "seed": {"class_type": "SeedVR2VideoUpscaler", "inputs": {}},
            "cleanup": {"class_type": "VRAMCleanup", "inputs": {}},
            "flux_model": {"class_type": "UNETLoader", "inputs": {}},
            "sample": {"class_type": "KSampler", "inputs": {}},
            "decode": {"class_type": "VAEDecode", "inputs": {}},
            "save": {"class_type": "SaveImage", "inputs": {}},
        }
        app_module.register_comfy_prompt("flux-progress", graph)
        app_module.update_comfy_progress("flux-progress", "seed", 3, 10)
        app_module.update_comfy_progress("flux-progress", "cleanup")
        app_module.update_comfy_progress("flux-progress", "sample", 4, 8)
        progress = app_module.enhanced_upscale_progress({
            "upscale_id": "flux-upscale-test", "prompt_id": "flux-progress", "status": "running",
        })
        self.assertEqual((progress["stage_index"], progress["stage_total"]), (4, 6))
        self.assertEqual(progress["stage_key"], "sampling")
        self.assertEqual((progress["step_current"], progress["step_total"]), (4, 8))
        self.assertTrue(progress["step_exact"])
        self.assertEqual([stage["state"] for stage in progress["stages"]], [
            "completed", "completed", "completed", "active", "pending", "pending",
        ])
        app_module.update_comfy_progress("flux-progress", "flux_model")
        self.assertEqual(app_module.COMFY_PROGRESS["flux-progress"]["stage_key"], "sampling")

    def test_enhanced_upscale_accumulates_repeated_tile_progress(self) -> None:
        graph = {
            "seed": {"class_type": "SeedVR2VideoUpscaler", "inputs": {}},
            "cleanup": {"class_type": "VRAMCleanup", "inputs": {}},
            "condition": {"class_type": "Flux2KleinEditTextEncode_EditUtils", "inputs": {}},
            "sample": {"class_type": "KSampler", "inputs": {}},
            "decode": {"class_type": "VAEDecode", "inputs": {}},
            "restore": {"class_type": "LayerUtility: ImageScaleRestore V2", "inputs": {}},
            "color": {"class_type": "easy imageColorMatch", "inputs": {}},
            "assembly": {"class_type": "TTP_Image_Assy", "inputs": {}},
            "grain": {"class_type": "LayerFilter: AddGrain", "inputs": {}},
            "brightness": {"class_type": "LayerColor: BrightnessContrastV2", "inputs": {}},
            "save": {"class_type": "SaveImage", "inputs": {}},
        }
        prompt_id = "flux-tile-progress"
        app_module.register_comfy_prompt(prompt_id, graph)
        observed = []
        for tile_index in range(9):
            app_module.process_comfy_progress_event({
                "type": "executing", "data": {
                    "prompt_id": prompt_id, "node": "seed",
                    "list_index": tile_index, "list_total": 9,
                },
            })
            for value in (0, 50, 100):
                app_module.process_comfy_progress_event({
                    "type": "progress", "data": {
                        "prompt_id": prompt_id, "node": "seed", "value": value, "max": 100,
                        "list_index": tile_index,
                    },
                })
                observed.append(app_module.COMFY_PROGRESS[prompt_id]["percent"])
        self.assertEqual(observed, sorted(observed))
        self.assertGreater(observed[-1], observed[2])
        event = app_module.COMFY_PROGRESS[prompt_id]
        self.assertEqual((event["tile_current"], event["tile_total"]), (9, 9))
        self.assertEqual((event["overall_current"], event["overall_total"]), (10.0, 60.0))

        app_module.update_comfy_progress(prompt_id, "cleanup")
        for node_id in ("condition", "sample", "decode", "restore", "color"):
            for tile_index in range(9):
                app_module.update_comfy_progress(
                    prompt_id, node_id, 1, 1, list_index=tile_index, list_total=9,
                )
                observed.append(app_module.COMFY_PROGRESS[prompt_id]["percent"])
        for node_id in ("assembly", "grain", "brightness", "save"):
            app_module.update_comfy_progress(prompt_id, node_id)
            observed.append(app_module.COMFY_PROGRESS[prompt_id]["percent"])
        self.assertEqual(observed, sorted(observed))
        self.assertLess(observed[-1], 100)

        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO upscale_jobs(upscale_id,prompt_id,parent_group_id,parent_item_id,status,submitted_at,parameters_json,engine,owner_id) VALUES(?,?,?,?,?,?,?,?,?)",
                ("flux-upscale", prompt_id, "group", "item", "running", now, "{}", "flux2", self.alice["user_id"]),
            )
            db.execute(
                "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at,started_at,prompt_id,profile_key) VALUES('upscale',?,?,?,?,?,?,?)",
                ("flux-upscale", self.alice["user_id"], "running", now, now, prompt_id, "upscale:flux2::4096"),
            )
        task = app_module.queue_snapshot(self.alice["user_id"])["tasks"][0]
        self.assertTrue(task["overall_exact"])
        self.assertEqual((task["tile_current"], task["tile_total"]), (9, 9))
        self.assertEqual((task["overall_current"], task["overall_total"]), (59.0, 60.0))
        app_module.mark_comfy_progress_complete(prompt_id)
        self.assertEqual(app_module.COMFY_PROGRESS[prompt_id]["percent"], 100)

    def test_queue_snapshot_exposes_phase_and_approximation(self) -> None:
        self.add_group("alice-running", self.alice["user_id"], "running", "phase-prompt")
        app_module.register_comfy_prompt("phase-prompt", {"sampler": {"class_type": "KSampler", "inputs": {}}})
        app_module.update_comfy_progress("phase-prompt", "sampler", 3, 8)
        task = app_module.queue_snapshot(self.alice["user_id"])["tasks"][0]
        self.assertEqual(task["progress_phase"], "正在采样当前图像")
        self.assertTrue(task["progress_approximate"])
        self.assertTrue(task["progress_exact"])
        self.assertEqual((task["step_current"], task["step_total"]), (3, 8))
        self.assertEqual((task["item_current"], task["item_total"]), (1, 1))
        self.assertEqual(task["progress_phase_key"], "sampling")
        self.assertGreater(task["item_progress"], 0)
        self.assertTrue(task["item_progress_approximate"])
        self.assertGreaterEqual(task["elapsed_seconds"], 0)
        self.assertGreaterEqual(task["item_elapsed_seconds"], 0)
        self.assertGreaterEqual(task["queue_elapsed_seconds"], 0)
        self.assertGreater(task["progress"], 0)

    def test_h3_queue_snapshot_exposes_runtime_risk_without_prompt(self) -> None:
        self.add_group("h3-running", self.alice["user_id"], "running", "h3-prompt")
        parameters = {
            "count": 1,
            "runtime_profile_version": app_module.H3_RUNTIME_PROFILE_VERSION,
            "vram": {"risk": "medium", "reason": "local calibration"},
        }
        app_module.db_execute(
            "UPDATE generation_groups SET workflow_key='minimax-h3',parameters_json=? WHERE group_id='h3-running'",
            (json.dumps(parameters),),
        )
        task = app_module.queue_snapshot(self.alice["user_id"])["tasks"][0]
        self.assertEqual(task["label"], "MiniMax H3 视频生成")
        self.assertEqual(task["vram_risk"], "medium")
        self.assertEqual(task["runtime_profile_version"], app_module.H3_RUNTIME_PROFILE_VERSION)
        self.assertNotIn("private prompt", json.dumps(task, ensure_ascii=False))

    def test_h3_estimate_uses_completed_nearby_history_without_formula_fallback(self) -> None:
        profile = app_module.queue_profile("generation", {
            "performance_tier": "standard", "mode": "i2v", "megapixels": 0.7,
            "width": 608, "height": 1088, "frames": 124, "steps": 8,
            "turbo_lora": app_module.H3_TURBO_LORAS[0], "low_vram": False, "loras": [],
        }, "minimax-h3")
        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            for index, seconds in enumerate((101, 112, 123, 710)):
                sample_profile = profile if index < 3 else profile.replace("mode=i2v", "mode=t2v").replace("frames=124", "frames=294")
                db.execute(
                    "INSERT INTO task_queue(task_kind,record_id,owner_id,state,queued_at,started_at,finished_at,profile_key) VALUES('generation',?,?,?,?,?,?,?)",
                    (f"h3-history-{index}", self.alice["user_id"], "completed", now, now, now, sample_profile),
                )
                db.execute(
                    "UPDATE task_queue SET finished_at=datetime(started_at, ? || ' seconds') WHERE record_id=?",
                    (seconds, f"h3-history-{index}"),
                )
        estimate = app_module.h3_history_estimate(profile)
        self.assertTrue(estimate["available"])
        self.assertEqual(estimate["samples"], 3)
        self.assertLess(estimate["high"], 200)
        self.assertNotEqual((estimate["low"], estimate["high"]), app_module.queue_default_estimate(profile))

    def test_h3_without_completed_history_has_no_numeric_queue_estimate(self) -> None:
        profile = "generation:minimax-h3:tier=standard:mode=t2v:mp=0.6:px=0.64:frames=124:steps=6:turbo=v4:low=0:loras=0:runtime=comfy032-h3-v1"
        estimate = app_module.queue_estimate(profile)
        self.assertEqual((estimate["low"], estimate["high"], estimate["confidence"]), (0, 0, "none"))

    def test_current_comfy_progress_state_keeps_h3_sampler_step_counter(self) -> None:
        prompt_id = "h3-progress-state"
        app_module.register_comfy_prompt(prompt_id, {
            "125": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
            "135": {"class_type": "MiniMaxH3TurboSampler", "inputs": {}},
        })
        app_module.process_comfy_progress_event({
            "type": "progress_state",
            "data": {"prompt_id": prompt_id, "nodes": {
                "125": {"node_id": "125", "state": "running", "value": 6, "max": 8},
            }},
        })
        event = app_module.COMFY_PROGRESS[prompt_id]
        self.assertEqual((event["value"], event["max"], event["phase"]), (6, 8, "sampling"))

    def test_progress_listener_has_a_reconnect_watchdog(self) -> None:
        source = inspect.getsource(app_module.comfy_progress_listener)
        self.assertIn("asyncio.wait_for(socket.recv(), timeout=15)", source)

    def test_multi_image_progress_separates_group_and_current_item(self) -> None:
        queue_id = self.add_group("multi-running", self.alice["user_id"], "running", "second-prompt")
        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute("UPDATE generation_groups SET parameters_json=? WHERE group_id='multi-running'", (json.dumps({"count": 4}),))
            db.execute(
                "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,completed_at,group_id,owner_id,logical_index) VALUES(?,?,?,?,?,?,?,?,?)",
                ("first-prompt", "qwen2511-modular-flux2", "private prompt", "completed", now, now, "multi-running", self.alice["user_id"], 0),
            )
            db.execute("UPDATE jobs SET logical_index=1 WHERE prompt_id='second-prompt'")
            started = datetime.now(timezone.utc) - timedelta(minutes=8)
            item_started = datetime.now(timezone.utc) - timedelta(minutes=2)
            db.execute(
                "UPDATE task_queue SET started_at=?,item_started_at=? WHERE queue_id=?",
                (started.isoformat(), item_started.isoformat(), queue_id),
            )
        app_module.register_comfy_prompt("second-prompt", {"sampler": {"class_type": "KSampler", "inputs": {}}})
        app_module.update_comfy_progress("second-prompt", "sampler", 4, 8)

        task = app_module.queue_snapshot(self.alice["user_id"])["tasks"][0]

        self.assertEqual((task["item_current"], task["item_total"]), (2, 4))
        self.assertGreater(task["progress"], 25)
        self.assertLess(task["progress"], 50)
        self.assertGreater(task["item_progress"], task["progress"])
        self.assertGreaterEqual(task["elapsed_seconds"], 480)
        self.assertGreaterEqual(task["item_elapsed_seconds"], 120)
        self.assertIn("第 2/4 张图", task["progress_phase"])

    def test_identity_phase_names_model_and_dual_reference_encoding(self) -> None:
        queue_id = self.add_group("identity-running", self.alice["user_id"], "running", "identity-prompt")
        app_module.db_execute(
            "UPDATE generation_groups SET workflow_key='krea-identity-edit',parameters_json=? WHERE group_id='identity-running'",
            (json.dumps({"count": 1, "krea_model": "official_turbo", "reference_mode": "face_swap"}),),
        )
        app_module.register_comfy_prompt("identity-prompt", {
            "load": {"class_type": "UNETLoader", "inputs": {}},
            "encode": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {}},
        })
        app_module.update_comfy_progress("identity-prompt", "load")
        loading = app_module.queue_snapshot(self.alice["user_id"])["tasks"][0]
        self.assertEqual(loading["label"], "Krea2 双参考图")
        self.assertEqual(loading["progress_phase"], "正在加载 Krea Turbo 与 Identity LoRA")

        app_module.update_comfy_progress("identity-prompt", "encode")
        encoding = app_module.queue_snapshot(self.alice["user_id"])["tasks"][0]
        self.assertEqual(encoding["progress_phase"], "正在编码图1场景、图2人物参考与提示词")
        self.assertEqual(encoding["progress_phase_key"], "encoding")

    def test_generation_profiles_include_resolution_and_keep_legacy_fallback(self) -> None:
        qwen = app_module.queue_profile(
            "generation",
            {"count": 3, "stage1_scale_megapixels": 1.25},
            "qwen2511-modular-flux2",
        )
        krea = app_module.queue_profile(
            "generation",
            {"count": 2, "width": 1920, "height": 1080, "krea_model": "official_turbo"},
            "krea-identity-edit",
        )
        self.assertIn(":mp=1.25:", qwen)
        self.assertIn(":mp=2:", krea)
        self.assertEqual(
            app_module.queue_profile_candidates(qwen)[1],
            "generation:qwen2511-modular-flux2:count=3",
        )

    def test_running_remaining_estimate_never_collapses_to_zero(self) -> None:
        estimate = {
            "low": 80, "high": 140, "confidence": "high",
            "samples": 5, "durations": [80, 90, 100, 120, 140],
        }
        low, high = app_module.queue_remaining_estimate(estimate, 500)
        self.assertGreaterEqual(low, 10)
        self.assertGreater(high, low)

    def test_waiting_eta_uses_running_tasks_remaining_time(self) -> None:
        running_id = self.add_group("alice-running", self.alice["user_id"], "running", "running-prompt")
        self.add_group("bob-waiting", self.bob["user_id"])
        started = datetime.now(timezone.utc) - timedelta(seconds=70)
        app_module.db_execute("UPDATE task_queue SET started_at=? WHERE queue_id=?", (started.isoformat(), running_id))
        estimate = {
            "low": 100, "high": 140, "confidence": "high",
            "samples": 5, "durations": [100, 110, 120, 130, 140],
        }
        with patch.object(app_module, "queue_estimate", return_value=estimate):
            waiting = app_module.queue_snapshot(self.alice["user_id"])["tasks"][1]
        self.assertGreater(waiting["eta_low"], 0)
        self.assertLess(waiting["eta_low"], 100)
        self.assertEqual(waiting["eta_confidence"], "high")

    def test_stalled_model_initialization_has_a_ten_minute_limit(self) -> None:
        queue_id = self.add_group("stalled-load", self.alice["user_id"], "running", "stalled-prompt")
        started = datetime.now(timezone.utc) - timedelta(minutes=11)
        app_module.db_execute("UPDATE task_queue SET started_at=? WHERE queue_id=?", (started.isoformat(), queue_id))
        row = app_module.queue_row(queue_id)
        app_module.register_comfy_prompt("stalled-prompt", {
            "load": {"class_type": "UNETLoader", "inputs": {}},
            "sample": {"class_type": "KSampler", "inputs": {}},
        })
        self.assertIn("10", app_module.running_task_timeout_reason(row) or "")

        app_module.update_comfy_progress("stalled-prompt", "sample", 1, 8)
        self.assertIsNone(app_module.running_task_timeout_reason(row))

    def test_h3_missing_local_pre_sampler_heartbeat_triggers_recovery(self) -> None:
        queue_id = self.add_group("h3-first-step", self.alice["user_id"], "running", "h3-first-step-prompt")
        started = datetime.now(timezone.utc) - timedelta(minutes=3, seconds=5)
        app_module.db_execute(
            "UPDATE task_queue SET started_at=?,item_started_at=?,profile_key=? WHERE queue_id=?",
            (started.isoformat(), started.isoformat(), "generation:minimax-h3:tier=standard", queue_id),
        )
        self.assertTrue(app_module.h3_first_sample_stalled(app_module.queue_row(queue_id)))
        with patch.object(app_module, "recover_h3_stall", new=AsyncMock()) as recover:
            asyncio.run(app_module.reconcile_running_task(app_module.queue_row(queue_id)))
        recover.assert_awaited_once()
        (app_module.H3_HEARTBEAT_DIR / "h3-first-step-prompt.json").write_text(json.dumps({
            "prompt_id": "h3-first-step-prompt", "stage": "sampler_ready", "updated_at": 0,
        }), encoding="utf-8")
        self.assertFalse(app_module.h3_first_sample_stalled(app_module.queue_row(queue_id)))

    def test_running_generation_must_be_stopped_before_deletion(self) -> None:
        queue_id = self.add_group("delete-running", self.alice["user_id"], "running", "delete-prompt")
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            with self.assertRaises(HTTPException) as blocked:
                asyncio.run(app_module.delete_job("delete-running"))
        finally:
            app_module.CURRENT_USER.reset(token)
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertIsNotNone(app_module.unrestricted_group("delete-running"))
        self.assertEqual(app_module.queue_row(queue_id)["state"], "running")

    def test_task_and_image_recycle_entries_are_private_and_restore_independently(self) -> None:
        self.add_h3_group("recycle-history", self.alice["user_id"], {"count": 1, "generation_seeds": [707]})
        image = {"filename": "recycle.mp4", "subfolder": "mobile/minimax-h3", "type": "output"}
        item = {"id": "recycle-item", "prompt_id": "recycle-child", "final": image, "stage1": None}
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,completed_at,parameters_json,outputs_json,storage_scope,group_id,owner_id,logical_index) "
                "VALUES('recycle-child','minimax-h3','prompt','completed',?,?,?,?,'mobile','recycle-history',?,0)",
                (app_module.utc_now(), app_module.utc_now(), json.dumps({"seed": 707}), json.dumps({"final": [image], "stage1": []}), self.alice["user_id"]),
            )
            db.execute(
                "UPDATE generation_groups SET items_json=?,outputs_json=? WHERE group_id='recycle-history'",
                (json.dumps([item]), json.dumps(app_module.outputs_from_items([item]))),
            )
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            image_entry = app_module.trash_item("recycle-history", "recycle-item")
            task_entry = app_module.trash_job("recycle-history")
            entries = asyncio.run(app_module.recycle_bin_entries())
        finally:
            app_module.CURRENT_USER.reset(token)
        self.assertEqual({entry["kind"] for entry in entries}, {"task", "image"})
        self.assertIsNotNone(app_module.unrestricted_group("recycle-history"))

        token = app_module.CURRENT_USER.set(self.bob)
        try:
            self.assertEqual(asyncio.run(app_module.recycle_bin_entries()), [])
            with self.assertRaises(HTTPException) as denied:
                asyncio.run(app_module.restore_recycle_entry(task_entry["entry_id"]))
        finally:
            app_module.CURRENT_USER.reset(token)
        self.assertEqual(denied.exception.status_code, 404)

        token = app_module.CURRENT_USER.set(self.alice)
        try:
            asyncio.run(app_module.restore_recycle_entry(task_entry["entry_id"]))
            after_task_restore = app_module.public_group(app_module.unrestricted_group("recycle-history"))
            self.assertEqual(after_task_restore["items"], [])
            with sqlite3.connect(app_module.DATABASE) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM recycle_bin WHERE entry_id=?", (image_entry["entry_id"],)).fetchone()[0], 1)
            asyncio.run(app_module.restore_recycle_entry(image_entry["entry_id"]))
            after_image_restore = app_module.public_group(app_module.unrestricted_group("recycle-history"))
        finally:
            app_module.CURRENT_USER.reset(token)
        self.assertEqual([entry["id"] for entry in after_image_restore["items"]], ["recycle-item"])
        self.assertEqual(after_image_restore["parameters"]["generation_seeds"], ["707"])

    def test_trashed_task_keeps_files_until_expiry_then_purges_everything(self) -> None:
        self.add_h3_group("expiring-task", self.alice["user_id"], {"count": 1, "generation_seeds": [808]})
        image = {"filename": "expires.mp4", "subfolder": "mobile/minimax-h3", "type": "output"}
        item = {"id": "expires-item", "prompt_id": "expires-child", "final": image, "stage1": None}
        output_path = app_module.OUTPUT_DIR / image["subfolder"] / image["filename"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"video")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,completed_at,parameters_json,outputs_json,storage_scope,group_id,owner_id,logical_index) "
                "VALUES('expires-child','minimax-h3','prompt','completed',?,?,?,?,'mobile','expiring-task',?,0)",
                (app_module.utc_now(), app_module.utc_now(), json.dumps({"seed": 808}), json.dumps({"final": [image], "stage1": []}), self.alice["user_id"]),
            )
            db.execute(
                "UPDATE generation_groups SET items_json=?,outputs_json=? WHERE group_id='expiring-task'",
                (json.dumps([item]), json.dumps(app_module.outputs_from_items([item]))),
            )
            collection_id = db.execute(
                "INSERT INTO favorite_collections(name,created_at,owner_id) VALUES('Keep',?,?)",
                (app_module.utc_now(), self.alice["user_id"]),
            ).lastrowid
            db.execute(
                "INSERT INTO collection_memberships(collection_id,record_id) VALUES(?, 'expiring-task')",
                (collection_id,),
            )

        token = app_module.CURRENT_USER.set(self.alice)
        try:
            trashed = app_module.trash_job("expiring-task")
        finally:
            app_module.CURRENT_USER.reset(token)
        self.assertGreaterEqual(
            (app_module.parse_utc_timestamp(trashed["purge_at"]) - datetime.now(timezone.utc)).total_seconds(),
            14 * 60,
        )
        self.assertIsNotNone(app_module.unrestricted_group("expiring-task"))
        self.assertIsNotNone(app_module.db_job("expires-child"))
        self.assertTrue(output_path.exists())
        with sqlite3.connect(app_module.DATABASE) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_memberships WHERE record_id='expiring-task'").fetchone()[0], 1)
            db.execute(
                "UPDATE recycle_bin SET purge_at=? WHERE entry_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), trashed["entry_id"]),
            )

        self.assertEqual(app_module.purge_expired_recycle_entries(self.alice["user_id"]), 1)
        self.assertIsNone(app_module.unrestricted_group("expiring-task"))
        self.assertIsNone(app_module.db_job("expires-child"))
        self.assertFalse(output_path.exists())
        with sqlite3.connect(app_module.DATABASE) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_memberships WHERE record_id='expiring-task'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recycle_bin WHERE entry_id=?", (trashed["entry_id"],)).fetchone()[0], 0)

    def test_expired_image_does_not_shorten_independent_task_restore_window(self) -> None:
        self.add_h3_group("independent-expiry", self.alice["user_id"], {"count": 1, "generation_seeds": [909]})
        image = {"filename": "independent.mp4", "subfolder": "mobile/minimax-h3", "type": "output"}
        item = {"id": "independent-item", "prompt_id": "independent-child", "final": image, "stage1": None}
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,completed_at,parameters_json,outputs_json,storage_scope,group_id,owner_id,logical_index) "
                "VALUES('independent-child','minimax-h3','prompt','completed',?,?,?,?,'mobile','independent-expiry',?,0)",
                (app_module.utc_now(), app_module.utc_now(), json.dumps({"seed": 909}), json.dumps({"final": [image], "stage1": []}), self.alice["user_id"]),
            )
            db.execute(
                "UPDATE generation_groups SET items_json=?,outputs_json=? WHERE group_id='independent-expiry'",
                (json.dumps([item]), json.dumps(app_module.outputs_from_items([item]))),
            )
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            image_entry = app_module.trash_item("independent-expiry", "independent-item")
            task_entry = app_module.trash_job("independent-expiry")
            with sqlite3.connect(app_module.DATABASE) as db:
                db.execute(
                    "UPDATE recycle_bin SET purge_at=? WHERE entry_id=?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), image_entry["entry_id"]),
                )
            self.assertEqual(app_module.purge_expired_recycle_entries(self.alice["user_id"]), 1)
            self.assertIsNotNone(app_module.unrestricted_group("independent-expiry"))
            with sqlite3.connect(app_module.DATABASE) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM recycle_bin WHERE entry_id=?", (task_entry["entry_id"],)).fetchone()[0], 1)
            restored = asyncio.run(app_module.restore_recycle_entry(task_entry["entry_id"]))
            self.assertTrue(restored["restored"])
            self.assertEqual(app_module.public_group(app_module.unrestricted_group("independent-expiry"))["items"], [])
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_deleting_first_batch_result_keeps_running_group(self) -> None:
        queue_id = self.add_group("active-batch", self.alice["user_id"], "running", "second-prompt")
        image = {"filename": "first.mp4", "subfolder": "mobile/minimax-h3", "type": "output"}
        item = {"id": "first-item", "prompt_id": "first-prompt", "final": image, "stage1": None}
        output_path = app_module.OUTPUT_DIR / image["subfolder"] / image["filename"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"video")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "UPDATE generation_groups SET workflow_key='minimax-h3',parameters_json=?,items_json=?,outputs_json=? WHERE group_id='active-batch'",
                (json.dumps({"count": 3, "generation_seeds": [11, 22, 33]}), json.dumps([item]), json.dumps({"final": [image], "stage1": []})),
            )
            db.execute(
                "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,completed_at,outputs_json,storage_scope,group_id,owner_id,logical_index) "
                "VALUES('first-prompt','minimax-h3','prompt','completed',?,?,?,'mobile','active-batch',?,0)",
                (app_module.utc_now(), app_module.utc_now(), json.dumps({"final": [image], "stage1": []}), self.alice["user_id"]),
            )
            db.execute("UPDATE jobs SET parameters_json=? WHERE prompt_id='first-prompt'", (json.dumps({"seed": 11}),))
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            result = app_module.hard_delete_item("active-batch", "first-item")
        finally:
            app_module.CURRENT_USER.reset(token)
        group = app_module.unrestricted_group("active-batch")
        public = app_module.public_group(group)
        parameters = json.loads(group["parameters_json"])
        self.assertEqual(result, {"deleted": True, "group_deleted": False, "remaining": 0})
        self.assertIsNotNone(group)
        self.assertEqual(json.loads(group["items_json"]), [])
        self.assertEqual(app_module.queue_row(queue_id)["state"], "running")
        self.assertEqual(app_module.db_job("first-prompt")["status"], "completed")
        self.assertEqual(app_module.db_job("first-prompt")["outputs_json"], '{"final": [], "stage1": []}')
        self.assertEqual(parameters["generation_seeds"], [11, 22, 33])
        self.assertEqual(parameters[app_module.DELETED_SEED_INDICES_KEY], [0])
        self.assertEqual(public["parameters"]["generation_seeds"], ["22", "33"])
        self.assertNotIn(app_module.DELETED_SEED_INDICES_KEY, public["parameters"])
        self.assertFalse(output_path.exists())

    def test_deleting_history_item_removes_its_generation_seed(self) -> None:
        seeds = [101, 202, 303]
        self.add_h3_group("seed-history", self.alice["user_id"], {
            "count": 3,
            "generation_seeds": seeds,
        })
        items = []
        for index, seed in enumerate(seeds):
            prompt_id = f"seed-prompt-{index}"
            image = {
                "filename": f"seed-result-{index}.mp4",
                "subfolder": "mobile/minimax-h3",
                "type": "output",
            }
            item = {
                "id": f"seed-item-{index}",
                "prompt_id": prompt_id,
                "final": image,
                "stage1": None,
            }
            items.append(item)
            output_path = app_module.OUTPUT_DIR / image["subfolder"] / image["filename"]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"video")
            with sqlite3.connect(app_module.DATABASE) as db:
                db.execute(
                    "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,completed_at,parameters_json,outputs_json,storage_scope,group_id,owner_id,logical_index) "
                    "VALUES(?,'minimax-h3','prompt','completed',?,?,?,?,'mobile','seed-history',?,?)",
                    (
                        prompt_id,
                        app_module.utc_now(),
                        app_module.utc_now(),
                        json.dumps({"seed": seed}),
                        json.dumps({"final": [image], "stage1": []}),
                        self.alice["user_id"],
                        index,
                    ),
                )
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "UPDATE generation_groups SET items_json=?,outputs_json=? WHERE group_id='seed-history'",
                (json.dumps(items), json.dumps(app_module.outputs_from_items(items))),
            )
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            result = app_module.trash_item("seed-history", "seed-item-1")
            public = app_module.public_group(app_module.unrestricted_group("seed-history"))
            stored = json.loads(app_module.unrestricted_group("seed-history")["parameters_json"])
            self.assertTrue(result["trashed"])
            self.assertEqual(result["remaining"], 2)
            self.assertEqual(stored["generation_seeds"], [101, 202, 303])
            self.assertEqual(public["parameters"]["generation_seeds"], ["101", "303"])
            self.assertEqual([item["id"] for item in public["items"]], ["seed-item-0", "seed-item-2"])
            self.assertTrue((app_module.OUTPUT_DIR / "mobile/minimax-h3/seed-result-1.mp4").exists())

            restored = asyncio.run(app_module.restore_recycle_entry(result["entry_id"]))
            self.assertTrue(restored["restored"])
            restored_public = app_module.public_group(app_module.unrestricted_group("seed-history"))
            self.assertEqual(restored_public["parameters"]["generation_seeds"], ["101", "202", "303"])
            self.assertEqual([item["id"] for item in restored_public["items"]], ["seed-item-0", "seed-item-1", "seed-item-2"])

            expiring = app_module.trash_item("seed-history", "seed-item-1")
            with sqlite3.connect(app_module.DATABASE) as db:
                db.execute(
                    "UPDATE recycle_bin SET purge_at=? WHERE entry_id=?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), expiring["entry_id"]),
                )
            self.assertEqual(app_module.purge_expired_recycle_entries(self.alice["user_id"]), 1)
        finally:
            app_module.CURRENT_USER.reset(token)
        stored = json.loads(app_module.unrestricted_group("seed-history")["parameters_json"])
        self.assertEqual(stored["generation_seeds"], [101, 303])
        self.assertFalse((app_module.OUTPUT_DIR / "mobile/minimax-h3/seed-result-1.mp4").exists())

    def test_terminal_history_repairs_seed_lists_left_by_older_deletions(self) -> None:
        seeds = [101, 202, 303]
        self.add_h3_group("legacy-seed-history", self.alice["user_id"], {
            "count": 3,
            "generation_seeds": seeds,
        })
        surviving = {
            "id": "legacy-item-1",
            "prompt_id": "legacy-prompt-1",
            "final": {"filename": "surviving.mp4", "subfolder": "mobile/minimax-h3", "type": "output"},
            "stage1": None,
        }
        with sqlite3.connect(app_module.DATABASE) as db:
            for index, seed in enumerate(seeds):
                outputs = {"final": [surviving["final"]] if index == 1 else [], "stage1": []}
                db.execute(
                    "INSERT INTO jobs(prompt_id,workflow_key,prompt_text,status,submitted_at,completed_at,parameters_json,outputs_json,storage_scope,group_id,owner_id,logical_index) "
                    "VALUES(?,'minimax-h3','prompt','completed',?,?,?,?,?,'legacy-seed-history',?,?)",
                    (
                        f"legacy-prompt-{index}", app_module.utc_now(), app_module.utc_now(),
                        json.dumps({"seed": seed}), json.dumps(outputs), "mobile",
                        self.alice["user_id"], index,
                    ),
                )
            db.execute(
                "UPDATE generation_groups SET items_json=?,outputs_json=? WHERE group_id='legacy-seed-history'",
                (json.dumps([surviving]), json.dumps(app_module.outputs_from_items([surviving]))),
            )
        public = app_module.public_group(app_module.unrestricted_group("legacy-seed-history"))
        self.assertEqual(public["parameters"]["generation_seeds"], ["202"])
        self.assertTrue(app_module.synchronize_terminal_group_seeds("legacy-seed-history"))
        stored = json.loads(app_module.unrestricted_group("legacy-seed-history")["parameters_json"])
        self.assertEqual(stored["generation_seeds"], [202])

    def test_successful_orphaned_generation_queue_is_closed(self) -> None:
        queue_id = self.add_group("deleted-after-output", self.alice["user_id"], "running", "orphan-prompt")
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute("DELETE FROM jobs WHERE group_id='deleted-after-output'")
            db.execute("DELETE FROM generation_groups WHERE group_id='deleted-after-output'")
        history = {
            "orphan-prompt": {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {},
            },
        }
        with patch.object(app_module, "comfy_json", new=AsyncMock(return_value=history)) as comfy:
            asyncio.run(app_module.reconcile_running_task(app_module.queue_row(queue_id)))
        closed = app_module.queue_row(queue_id)
        self.assertEqual(closed["state"], "completed")
        self.assertEqual(closed["progress_value"], 100)
        self.assertIsNone(closed["prompt_id"])
        comfy.assert_awaited_once_with("GET", "/history/orphan-prompt")

    def test_h3_stale_sampler_heartbeat_recovers_but_final_step_does_not(self) -> None:
        queue_id = self.add_group("h3-heartbeat", self.alice["user_id"], "running", "h3-heartbeat-prompt")
        app_module.db_execute(
            "UPDATE task_queue SET profile_key=? WHERE queue_id=?",
            ("generation:minimax-h3:tier=standard", queue_id),
        )
        heartbeat_path = app_module.H3_HEARTBEAT_DIR / "h3-heartbeat-prompt.json"
        heartbeat_path.write_text(json.dumps({
            "prompt_id": "h3-heartbeat-prompt", "stage": "forward_started",
            "step": 2, "total": 8, "updated_at": 0,
        }), encoding="utf-8")
        self.assertTrue(app_module.h3_sampler_heartbeat_stalled(app_module.queue_row(queue_id)))
        with patch.object(app_module, "recover_h3_stall", new=AsyncMock()) as recover:
            asyncio.run(app_module.reconcile_running_task(app_module.queue_row(queue_id)))
        recover.assert_awaited_once()

        heartbeat_path.write_text(json.dumps({
            "prompt_id": "h3-heartbeat-prompt", "stage": "step_completed",
            "step": 8, "total": 8, "updated_at": 0,
        }), encoding="utf-8")
        self.assertFalse(app_module.h3_sampler_heartbeat_stalled(app_module.queue_row(queue_id)))

    def test_h3_cooldown_blocks_only_matching_runtime_settings(self) -> None:
        values = {"h3_model": "original_int8", "mode": "i2v", "width": 1056, "height": 608, "frames": 124, "steps": 8, "turbo_lora": "turbo", "lora_strength": 1, "low_vram": False, "loras": []}
        fingerprint = app_module.h3_settings_fingerprint(values)
        until = datetime.now(timezone.utc) + timedelta(minutes=30)
        app_module.db_execute(
            "INSERT INTO h3_setting_cooldowns(settings_fingerprint,blocked_until,reason,created_at) VALUES(?,?,?,?)",
            (fingerprint, until.isoformat(), "test", app_module.utc_now()),
        )
        self.assertGreater(app_module.h3_cooldown_remaining(values), 0)
        with self.assertRaises(HTTPException) as blocked:
            app_module.enforce_h3_cooldown(values)
        self.assertEqual(blocked.exception.status_code, 429)
        changed = dict(values, steps=6)
        self.assertEqual(app_module.h3_cooldown_remaining(changed), 0)

    def test_h3_lottery_rerun_locks_history_settings_and_generates_three_seeds(self) -> None:
        self.add_h3_group("h3-history", self.alice["user_id"])
        forged = {
            "negative_prompt": "new negative", "count": 8, "mode": "i2v", "megapixels": 1.0,
            "duration": 15, "steps": 12, "h3_model": "pinkcherry_06beta", "loras": [],
        }
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            with patch.object(app_module, "scheduler_tick", new=AsyncMock()):
                created = asyncio.run(app_module.submit_job(
                    workflow="minimax-h3", prompt="new prompt", settings=json.dumps(forged),
                    rerun_group_id="h3-history", h3_lottery_count="3",
                ))
        finally:
            app_module.CURRENT_USER.reset(token)
        values = json.loads(app_module.db_group(created["id"])["parameters_json"])
        self.assertEqual(values["count"], 3)
        self.assertTrue(values["random_seed"])
        self.assertEqual(values["megapixels"], 0.6)
        self.assertEqual(values["duration"], 5)
        self.assertEqual(values["steps"], 8)
        self.assertEqual(values["h3_model"], "original_int8")
        self.assertEqual(values["negative_prompt"], "new negative")
        self.assertEqual(values["rerun_source_group_id"], "h3-history")
        self.assertEqual(len(values["generation_seeds"]), 3)
        self.assertEqual(len(set(values["generation_seeds"])), 3)

    def test_h3_lottery_rejects_non_history_or_out_of_range_requests(self) -> None:
        self.add_h3_group("alice-h3-history", self.alice["user_id"])
        self.add_h3_group("bob-h3-history", self.bob["user_id"])
        spec = app_module.get_spec("minimax-h3")
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            with self.assertRaises(HTTPException) as missing:
                app_module.h3_rerun_lottery_values(spec, "{}", None, 2)
            self.assertEqual(missing.exception.status_code, 422)
            with self.assertRaises(HTTPException) as other_user:
                app_module.h3_rerun_lottery_values(spec, "{}", "bob-h3-history", 2)
            self.assertEqual(other_user.exception.status_code, 404)
            with self.assertRaises(HTTPException) as too_many:
                app_module.h3_rerun_lottery_count("4")
            self.assertEqual(too_many.exception.status_code, 400)
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_timed_out_task_is_interrupted_and_failed(self) -> None:
        queue_id = self.add_group("timed-out", self.alice["user_id"], "running", "timed-out-prompt")
        row = app_module.queue_row(queue_id)
        comfy = AsyncMock(side_effect=[
            {"queue_running": [[1, "timed-out-prompt", {}, {}, []]], "queue_pending": []},
            {},
        ])
        with patch.object(app_module, "comfy_json", comfy):
            asyncio.run(app_module.fail_timed_out_task(row, "timeout"))
        self.assertEqual(app_module.queue_row(queue_id)["state"], "failed")
        self.assertEqual(app_module.unrestricted_group("timed-out")["status"], "failed")
        self.assertEqual(app_module.db_job("timed-out-prompt")["status"], "failed")
        self.assertEqual(comfy.await_args_list[1].args, ("POST", "/interrupt"))

    def test_user_cannot_cancel_another_users_queue_item(self) -> None:
        queue_id = self.add_group("bob-one", self.bob["user_id"])
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(app_module.cancel_shared_queue_task(queue_id))
            self.assertEqual(raised.exception.status_code, 404)
        finally:
            app_module.CURRENT_USER.reset(token)

    def test_running_cancel_stays_cancelling_until_comfy_releases_prompt(self) -> None:
        queue_id = self.add_group("cancel-running", self.alice["user_id"], "running", "cancel-prompt")
        cancel_comfy = AsyncMock(side_effect=[
            {"queue_running": [[1, "cancel-prompt", {}, {}, []]], "queue_pending": []},
            {},
        ])
        with patch.object(app_module, "comfy_json", cancel_comfy):
            asyncio.run(app_module.cancel_queued_task(app_module.queue_row(queue_id)))
        cancelling = app_module.queue_row(queue_id)
        self.assertEqual(cancelling["state"], "cancelling")
        self.assertEqual(cancelling["prompt_id"], "cancel-prompt")
        self.assertEqual(app_module.unrestricted_group("cancel-running")["status"], "cancelling")
        self.assertEqual(app_module.db_job("cancel-prompt")["status"], "cancelling")
        self.assertEqual(cancel_comfy.await_args_list[1].args, ("POST", "/interrupt"))

        reconcile_comfy = AsyncMock(side_effect=[
            {"queue_running": [], "queue_pending": []},
            {},
            {},
        ])
        with patch.object(app_module, "comfy_json", reconcile_comfy):
            asyncio.run(app_module.reconcile_cancelling_task(cancelling))
        self.assertEqual(app_module.queue_row(queue_id)["state"], "cancelled")
        self.assertEqual(app_module.unrestricted_group("cancel-running")["status"], "cancelled")
        self.assertEqual(app_module.db_job("cancel-prompt")["status"], "cancelled")
        self.assertEqual(reconcile_comfy.await_args_list[2].args, ("POST", "/free"))

    def test_cancelled_generation_recovers_already_saved_output(self) -> None:
        queue_id = self.add_group("cancel-partial", self.alice["user_id"], "cancelling", "partial-prompt")
        saved = app_module.OUTPUT_DIR / "mobile" / "cancel-partial_00001_.png"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(b"partial")
        history = {"partial-prompt": {"status": {"status_str": "error"}, "outputs": {"save": {"images": [{"filename": saved.name, "subfolder": "mobile", "type": "output"}]}}}}
        with patch.object(app_module, "comfy_json", new=AsyncMock(return_value=history)):
            asyncio.run(app_module.finalize_cancelled_task(app_module.queue_row(queue_id)))
        group = app_module.unrestricted_group("cancel-partial")
        self.assertEqual(group["status"], "cancelled")
        self.assertEqual(len(app_module.stored_items(group)), 1)
        self.assertEqual(app_module.stored_items(group)[0]["final"]["filename"], saved.name)

    def test_running_cancel_sends_global_interrupt_only_once(self) -> None:
        queue_id = self.add_group("cancel-once", self.alice["user_id"], "running", "cancel-once-prompt")
        first = AsyncMock(side_effect=[
            {"queue_running": [[1, "cancel-once-prompt", {}, {}, []]], "queue_pending": []},
            {},
        ])
        with patch.object(app_module, "comfy_json", first):
            asyncio.run(app_module.cancel_queued_task(app_module.queue_row(queue_id)))
        cancelling = app_module.queue_row(queue_id)
        self.assertEqual(cancelling["interrupted_count"], 1)
        self.assertIsNotNone(cancelling["last_interrupt_at"])

        still_running = AsyncMock(return_value={
            "queue_running": [[1, "cancel-once-prompt", {}, {}, []]],
            "queue_pending": [],
        })
        with patch.object(app_module, "comfy_json", still_running):
            asyncio.run(app_module.reconcile_cancelling_task(cancelling))
            asyncio.run(app_module.reconcile_cancelling_task(app_module.queue_row(queue_id)))

        self.assertEqual(still_running.await_count, 2)
        self.assertTrue(all(call.args == ("GET", "/queue") for call in still_running.await_args_list))
        self.assertEqual(app_module.queue_row(queue_id)["interrupted_count"], 1)

    def test_krea_batch_keeps_model_resident_between_images(self) -> None:
        queue_id = self.add_group("krea-batch", self.alice["user_id"], "running", "krea-first")
        app_module.db_execute(
            "UPDATE generation_groups SET workflow_key='krea-identity-edit',parameters_json=? WHERE group_id='krea-batch'",
            (json.dumps({"count": 2, "stage1_seeds": [1, 2]}),),
        )
        app_module.db_execute(
            "UPDATE jobs SET workflow_key='krea-identity-edit',status='completed',completed_at=? WHERE prompt_id='krea-first'",
            (app_module.utc_now(),),
        )
        release = AsyncMock()
        dispatch = AsyncMock()
        with patch.object(app_module, "refresh_job", new=AsyncMock(return_value={"status": "completed"})), \
                patch.object(app_module, "release_krea_runtime", new=release), \
                patch.object(app_module, "dispatch_claimed_task", new=dispatch):
            asyncio.run(app_module.reconcile_running_task(app_module.queue_row(queue_id)))

        release.assert_not_awaited()
        dispatch.assert_awaited_once()
        self.assertEqual(app_module.queue_row(queue_id)["state"], "dispatching")

    def test_scheduler_recovers_a_legacy_cancelled_prompt_still_running_in_comfy(self) -> None:
        queue_id = self.add_group("legacy-cancel", self.alice["user_id"], "running", "legacy-prompt")
        app_module.db_execute(
            "UPDATE task_queue SET state='cancelled',prompt_id=NULL,finished_at=? WHERE queue_id=?",
            (app_module.utc_now(), queue_id),
        )
        app_module.db_execute(
            "UPDATE generation_groups SET status='cancelled',completed_at=? WHERE group_id='legacy-cancel'",
            (app_module.utc_now(),),
        )
        app_module.db_execute(
            "UPDATE jobs SET status='cancelled',completed_at=? WHERE prompt_id='legacy-prompt'",
            (app_module.utc_now(),),
        )
        comfy = AsyncMock(return_value={
            "queue_running": [[1, "legacy-prompt", {}, {}, []]],
            "queue_pending": [],
        })
        with patch.object(app_module, "comfy_json", comfy):
            asyncio.run(app_module.adopt_orphaned_cancelled_prompt())
        adopted = app_module.queue_row(queue_id)
        self.assertEqual(adopted["state"], "cancelling")
        self.assertEqual(adopted["prompt_id"], "legacy-prompt")
        self.assertEqual(app_module.unrestricted_group("legacy-cancel")["status"], "cancelling")
        self.assertEqual(app_module.db_job("legacy-prompt")["status"], "cancelling")

    def test_performance_mode_interrupts_managed_prompt_and_restores_waiting(self) -> None:
        queue_id = self.add_group("alice-running", self.alice["user_id"], "running", "managed-prompt")
        comfy = AsyncMock(side_effect=[
            {"queue_running": [[1, "managed-prompt", {}, {}, []]], "queue_pending": []},
            {},
        ])
        with patch.object(app_module, "comfy_json", comfy):
            asyncio.run(app_module.set_performance_mode(True, self.admin["user_id"]))
        paused = app_module.queue_row(queue_id)
        self.assertEqual(paused["state"], "paused")
        self.assertIsNone(paused["prompt_id"])
        self.assertEqual(app_module.db_group_jobs("alice-running"), [])
        self.assertEqual(comfy.await_args_list[1].args, ("POST", "/interrupt"))
        asyncio.run(app_module.set_performance_mode(False, self.admin["user_id"]))
        self.assertEqual(app_module.queue_row(queue_id)["state"], "waiting")

    def test_startup_adopts_existing_waiting_record(self) -> None:
        now = app_module.utc_now()
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO preprocess_jobs(preprocess_id,status,submitted_at,parameters_json,source_image_json,owner_id) VALUES(?,'queued',?,?,?,?)",
                ("pre-existing", now, json.dumps({"model": "3b", "resolution": 1080}), json.dumps({"filename": "source.png"}), self.alice["user_id"]),
            )
        app_module.adopt_existing_active_tasks()
        row = app_module.db_queue_record("preprocess", "pre-existing")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "waiting")

    def test_authenticated_submission_is_dispatched_through_queue(self) -> None:
        spec = app_module.get_spec("minimax-h3")
        values = app_module.workflow_descriptor(spec)["defaults"]
        values["count"] = 1
        settings = json.dumps(values)
        comfy = AsyncMock(side_effect=[
            {"queue_running": [], "queue_pending": []},
            {"SaveImage": {}},
            {"prompt_id": "managed-generation"},
        ])
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            with patch.object(app_module, "build_graph", return_value=({}, {})) as build_graph, patch.object(app_module, "comfy_json", comfy):
                group = asyncio.run(app_module.submit_job("minimax-h3", "a test video", settings, None, None, None))
        finally:
            app_module.CURRENT_USER.reset(token)
        queued = app_module.db_queue_record("generation", group["id"])
        self.assertEqual(queued["state"], "running")
        self.assertEqual(queued["prompt_id"], "managed-generation")
        self.assertEqual(app_module.db_group_jobs(group["id"])[0]["prompt_id"], "managed-generation")
        self.assertEqual(app_module.generation_expected(app_module.unrestricted_group(group["id"])), 1)
        dispatched = build_graph.call_args.args[1]
        self.assertEqual(dispatched["count"], 1)
        self.assertEqual(dispatched["seed"], 0)
        self.assertEqual(app_module.unrestricted_group(group["id"])["parameters_json"].count("generation_seeds"), 1)

    def test_flux2_upscale_uses_dispatcher(self) -> None:
        now = app_module.utc_now()
        edit = {"filename": "edit.png", "subfolder": "mobile/test", "type": "output"}
        edit_path = app_module.OUTPUT_DIR / edit["subfolder"] / edit["filename"]
        edit_path.parent.mkdir(parents=True)
        edit_path.write_bytes(b"edit")
        items = [{"id": "item-one", "prompt_id": "done", "final": edit, "stage1": None}]
        with sqlite3.connect(app_module.DATABASE) as db:
            db.execute(
                "INSERT INTO generation_groups(group_id,workflow_key,prompt_text,status,submitted_at,items_json,storage_scope,owner_id) VALUES(?,?,?,?,?,?,'mobile',?)",
                ("edit-group", "qwen2511-modular-flux2", "edit", "completed", now, json.dumps(items), self.alice["user_id"]),
            )
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            comfy = AsyncMock(side_effect=[{"queue_running": [], "queue_pending": []}, {"SaveImage": {}}, {"prompt_id": "managed-upscale"}])
            with patch.object(app_module, "build_enhanced_upscale_graph", return_value=({}, {})), patch.object(app_module, "comfy_json", comfy):
                upscale = asyncio.run(app_module.submit_upscale("edit-group", "item-one", {"engine": "flux2", "resolution": 2048}))
        finally:
            app_module.CURRENT_USER.reset(token)
        self.assertEqual(app_module.db_queue_record("upscale", upscale["id"])["prompt_id"], "managed-upscale")

    def test_authenticated_standalone_flux_upload_is_queued(self) -> None:
        body = BytesIO()
        Image.new("RGB", (4, 4), "white").save(body, "PNG")
        upload = UploadFile(BytesIO(body.getvalue()), filename="source.png", headers=Headers({"content-type": "image/png"}))
        token = app_module.CURRENT_USER.set(self.alice)
        try:
            comfy = AsyncMock(side_effect=[{"queue_running": [], "queue_pending": []}, {"SaveImage": {}}, {"prompt_id": "managed-flux"}])
            with patch.object(app_module, "build_enhanced_upscale_graph", return_value=({}, {})), patch.object(app_module, "comfy_json", comfy):
                result = asyncio.run(app_module.submit_standalone_flux_upscale("{}", upload))
        finally:
            app_module.CURRENT_USER.reset(token)
        queued = app_module.db_queue_record("upscale", result["id"])
        self.assertEqual(queued["state"], "running")
        self.assertEqual(queued["prompt_id"], "managed-flux")

    def test_local_control_uses_admin_and_persists_mode(self) -> None:
        with patch.object(control_module.asyncio, "sleep", new=AsyncMock()):
            asyncio.run(control_module.apply_mode(True))
        self.assertTrue(app_module.performance_mode_enabled())
        asyncio.run(control_module.apply_mode(False))
        self.assertFalse(app_module.performance_mode_enabled())


if __name__ == "__main__":
    unittest.main()
