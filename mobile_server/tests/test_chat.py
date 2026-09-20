from __future__ import annotations

import asyncio
from io import BytesIO
import gc
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

import mobile_server.app as app_module


class ChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        names = ("DATABASE", "DATA_DIR", "AVATAR_DIR", "COMFY_ROOT", "UPLOAD_DIR", "OUTPUT_DIR", "MOBILE_OUTPUT_DIR", "CHAT_MEDIA_DIR")
        self.originals = {name: getattr(app_module, name) for name in names}
        app_module.DATABASE = root / "chat.sqlite3"
        app_module.DATA_DIR = root / "data"
        app_module.AVATAR_DIR = app_module.DATA_DIR / "avatars"
        app_module.COMFY_ROOT = root / "ComfyUI"
        app_module.UPLOAD_DIR = app_module.COMFY_ROOT / "input" / "mobile_uploads"
        app_module.OUTPUT_DIR = root / "output"
        app_module.MOBILE_OUTPUT_DIR = app_module.OUTPUT_DIR / "mobile"
        app_module.CHAT_MEDIA_DIR = root / "chat_media"
        app_module.UPLOAD_DIR.mkdir(parents=True)
        app_module.MOBILE_OUTPUT_DIR.mkdir(parents=True)
        app_module.CHAT_MEDIA_DIR.mkdir(parents=True)
        app_module.init_database()
        self.alice = app_module.create_user_account("Alice", "alice-password-123")
        self.bob = app_module.create_user_account("Bob", "bob-password-12345")

    def tearDown(self) -> None:
        app_module.CURRENT_USER.set(None)
        app_module.CHAT_CONNECTIONS.clear()
        for name, value in self.originals.items():
            setattr(app_module, name, value)
        gc.collect()
        self.temporary.cleanup()

    def run_as(self, user: dict, awaitable):
        token = app_module.CURRENT_USER.set(user)
        try:
            return asyncio.run(awaitable)
        finally:
            app_module.CURRENT_USER.reset(token)

    def befriend(self) -> int:
        request = self.run_as(self.alice, app_module.create_friend_request({"username": "bOb"}))
        accepted = self.run_as(self.bob, app_module.accept_friend_request(request["id"]))
        return accepted["conversation_id"]

    def insert_image_group(self) -> tuple[str, str, Path]:
        folder = app_module.MOBILE_OUTPUT_DIR / "shared"
        folder.mkdir(parents=True)
        path = folder / "result.png"
        Image.new("RGB", (32, 32), "red").save(path)
        image = {"filename": path.name, "subfolder": "mobile/shared", "type": "output"}
        item = {"id": "item-1", "prompt_id": "prompt-1", "final": image, "stage1": None}
        app_module.db_execute(
            """INSERT INTO generation_groups(
                group_id,workflow_key,prompt_text,title,status,submitted_at,completed_at,
                outputs_json,parameters_json,items_json,storage_scope,owner_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "alice-group", "qwen2511-modular-flux2", "edit this", "Shared task", "completed",
                app_module.utc_now(), app_module.utc_now(), json.dumps({"final": [image], "stage1": []}),
                "{}", json.dumps([item]), "mobile", self.alice["user_id"],
            ),
        )
        return "alice-group", "item-1", path

    def insert_h3_group(self) -> tuple[str, str, Path]:
        folder = app_module.MOBILE_OUTPUT_DIR / "minimax-h3"
        folder.mkdir(parents=True)
        path = folder / "result.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)
        video = {
            "filename": path.name, "subfolder": "mobile/minimax-h3",
            "type": "output", "media_type": "video/mp4",
        }
        item = {"id": "video-1", "prompt_id": "prompt-h3", "final": video, "stage1": None}
        parameters = {
            "mode": "t2v", "aspect_ratio": "16:9", "megapixels": 0.6,
            "duration": 5, "steps": 6, "turbo_lora": app_module.H3_TURBO_LORAS[0],
        }
        app_module.db_execute(
            """INSERT INTO generation_groups(
                group_id,workflow_key,prompt_text,title,status,submitted_at,completed_at,
                outputs_json,parameters_json,items_json,storage_scope,owner_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "alice-h3", "minimax-h3", "A cinematic ocean sunrise", "Shared H3 video", "completed",
                app_module.utc_now(), app_module.utc_now(), json.dumps({"final": [video], "stage1": []}),
                json.dumps(parameters), json.dumps([item]), "mobile", self.alice["user_id"],
            ),
        )
        return "alice-h3", "video-1", path

    def test_exact_case_insensitive_search_and_friend_request(self) -> None:
        result = self.run_as(self.alice, app_module.chat_search("bOB"))
        self.assertEqual(result["user"]["username"], "Bob")
        missing = self.run_as(self.alice, app_module.chat_search("Bo"))
        self.assertIsNone(missing["user"])
        conversation_id = self.befriend()
        overview = self.run_as(self.alice, app_module.chat_overview())
        self.assertEqual(overview["conversations"][0]["id"], conversation_id)

    def test_chat_identity_payloads_include_avatar_and_group_ready_fields(self) -> None:
        app_module.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (256, 256), "navy").save(
            app_module.avatar_path(self.alice["user_id"]), "WEBP",
        )
        app_module.db_execute(
            "UPDATE users SET avatar_version=1 WHERE user_id=?",
            (self.alice["user_id"],),
        )
        conversation_id = self.befriend()
        message = self.run_as(
            self.alice,
            app_module.send_chat_message(
                conversation_id, {"body": "profile payload", "client_nonce": "profile-payload"},
            ),
        )
        self.assertEqual(message["sender"]["username"], "Alice")
        self.assertEqual(message["sender"]["avatar_version"], 1)
        self.assertIn("?v=1", message["sender"]["avatar_url"])

        overview = self.run_as(self.bob, app_module.chat_overview())
        conversation = overview["conversations"][0]
        self.assertEqual(conversation["kind"], "direct")
        self.assertEqual(conversation["display"]["title"], "Alice")
        self.assertEqual(conversation["display"]["avatar_url"], conversation["peer"]["avatar_url"])
        history = self.run_as(self.bob, app_module.chat_messages(conversation_id))
        self.assertEqual(history["conversation"]["kind"], "direct")
        self.assertEqual(history["messages"][0]["sender"]["id"], self.alice["user_id"])

    def test_conversation_overview_has_per_user_activity_indexes(self) -> None:
        with app_module.db_connect() as db:
            indexes = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='chat_conversations'"
            ).fetchall()}
        self.assertIn("idx_chat_conversation_user_a_activity", indexes)
        self.assertIn("idx_chat_conversation_user_b_activity", indexes)

    def test_messages_are_member_scoped_and_friend_delete_is_read_only(self) -> None:
        conversation_id = self.befriend()
        message = self.run_as(self.alice, app_module.send_chat_message(conversation_id, {"body": "hello", "client_nonce": "one"}))
        duplicate = self.run_as(self.alice, app_module.send_chat_message(conversation_id, {"body": "hello", "client_nonce": "one"}))
        self.assertEqual(message["id"], duplicate["id"])
        self.run_as(self.alice, app_module.delete_chat_friend(self.bob["user_id"]))
        history = self.run_as(self.bob, app_module.chat_messages(conversation_id))
        self.assertTrue(history["conversation"]["read_only"])
        with self.assertRaises(HTTPException) as raised:
            self.run_as(self.bob, app_module.send_chat_message(conversation_id, {"body": "blocked"}))
        self.assertEqual(raised.exception.status_code, 409)

    def test_message_pagination_supports_before_and_after_without_duplicates(self) -> None:
        conversation_id = self.befriend()
        sent = [
            self.run_as(self.alice, app_module.send_chat_message(
                conversation_id, {"body": f"message-{index}", "client_nonce": f"page-{index}"},
            ))
            for index in range(6)
        ]
        newest = self.run_as(self.bob, app_module.chat_messages(conversation_id, limit=3))
        self.assertEqual([item["id"] for item in newest["messages"]], [item["id"] for item in sent[-3:]])
        older = self.run_as(self.bob, app_module.chat_messages(
            conversation_id, before=newest["messages"][0]["id"], limit=3,
        ))
        self.assertEqual([item["id"] for item in older["messages"]], [item["id"] for item in sent[:3]])
        incremental = self.run_as(self.bob, app_module.chat_messages(
            conversation_id, after=sent[2]["id"], limit=10,
        ))
        self.assertEqual([item["id"] for item in incremental["messages"]], [item["id"] for item in sent[3:]])
        with self.assertRaises(HTTPException) as raised:
            self.run_as(self.bob, app_module.chat_messages(
                conversation_id, before=sent[2]["id"], after=sent[2]["id"],
            ))
        self.assertEqual(raised.exception.status_code, 400)

    def test_overview_and_anchor_window_follow_the_first_unread_message(self) -> None:
        conversation_id = self.befriend()
        sent = [
            self.run_as(self.alice, app_module.send_chat_message(
                conversation_id, {"body": f"unread-{index}", "client_nonce": f"unread-{index}"},
            ))
            for index in range(8)
        ]
        opened = self.run_as(self.bob, app_module.open_chat_conversation(
            conversation_id, {"last_message_id": sent[2]["id"]},
        ))
        self.assertEqual(opened["unread_total"], 5)
        self.assertEqual(opened["conversation"]["first_unread_message_id"], sent[3]["id"])
        overview = self.run_as(self.bob, app_module.chat_overview())
        conversation = overview["conversations"][0]
        self.assertEqual(conversation["last_opened_message_id"], sent[2]["id"])
        self.assertEqual(conversation["first_unread_message_id"], sent[3]["id"])
        self.assertEqual(conversation["unread"], 5)

        window = self.run_as(self.bob, app_module.chat_messages(
            conversation_id, anchor=sent[3]["id"], limit=5,
        ))
        self.assertEqual([message["id"] for message in window["messages"]], [
            message["id"] for message in sent[2:7]
        ])
        self.assertTrue(window["has_older"])
        self.assertTrue(window["has_newer"])

        self.run_as(self.bob, app_module.open_chat_conversation(
            conversation_id, {"last_message_id": sent[5]["id"]},
        ))
        updated = self.run_as(self.bob, app_module.chat_overview())["conversations"][0]
        self.assertEqual(updated["first_unread_message_id"], sent[6]["id"])
        self.assertEqual(updated["unread"], 2)

    def test_overview_includes_attachment_summary_and_unread_count(self) -> None:
        conversation_id = self.befriend()
        group_id, _, _ = self.insert_image_group()
        message = self.run_as(self.alice, app_module.send_chat_message(conversation_id, {
            "attachment": {"kind": "task", "group_id": group_id},
        }))
        overview = self.run_as(self.bob, app_module.chat_overview())
        conversation = overview["conversations"][0]
        self.assertEqual(conversation["unread"], 1)
        self.assertEqual(conversation["last_message"]["id"], message["id"])
        self.assertEqual(conversation["last_message"]["attachment"]["kind"], "task")

    def test_websocket_message_event_contains_incremental_payload(self) -> None:
        conversation_id = self.befriend()
        captured: list[tuple[set[int], dict]] = []

        async def capture(user_ids, event):
            captured.append((set(user_ids), event))

        with patch.object(app_module, "emit_chat_event", side_effect=capture):
            message = self.run_as(self.alice, app_module.send_chat_message(conversation_id, {
                "body": "incremental", "client_nonce": "incremental-event",
            }))
        message_events = [event for _, event in captured if event.get("type") == "message"]
        self.assertEqual(len(message_events), 2)
        for event in message_events:
            self.assertEqual(event["message"], message)
            self.assertEqual(event["conversation_id"], conversation_id)
            self.assertEqual(event["conversation"]["last_message"]["id"], message["id"])
            self.assertIn("unread_total", event)

    def test_shared_task_import_is_receiver_owned_and_idempotent(self) -> None:
        conversation_id = self.befriend()
        group_id, _, _ = self.insert_image_group()
        message = self.run_as(self.alice, app_module.send_chat_message(conversation_id, {
            "attachment": {"kind": "task", "group_id": group_id},
        }))
        first = self.run_as(self.bob, app_module.import_chat_attachment(message["attachment"]["id"]))
        second = self.run_as(self.bob, app_module.import_chat_attachment(message["attachment"]["id"]))
        self.assertFalse(first["existing"])
        self.assertTrue(second["existing"])
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertEqual(first["job"]["origin"]["sender_name"], "Alice")

    def test_shared_task_detail_exposes_title_parameters_and_all_outputs(self) -> None:
        conversation_id = self.befriend()
        group_id, _, _ = self.insert_image_group()
        app_module.db_execute(
            "UPDATE generation_groups SET parameters_json=? WHERE group_id=?",
            (json.dumps({"steps": 8, "stage1_seed": 123, "negative_prompt": "bad"}), group_id),
        )
        message = self.run_as(self.alice, app_module.send_chat_message(conversation_id, {
            "attachment": {"kind": "task", "group_id": group_id},
        }))
        detail = self.run_as(self.bob, app_module.chat_attachment_detail(message["attachment"]["id"]))
        self.assertEqual(detail["title"], "Shared task")
        self.assertNotEqual(detail["workflow_name"], detail["workflow"])
        self.assertEqual(detail["parameters"]["stage1_seed"], 123)
        self.assertEqual([entry["role"] for entry in detail["files"]], ["final"])
        self.assertEqual(detail["items"][0]["id"], "item-1")
        self.assertEqual(detail["items"][0]["final"]["role"], "final")
        self.assertEqual(detail["items"][0]["details"], [])
        self.assertEqual(detail["items"][0]["upscales"], [])
        self.assertEqual(detail["sources"], {})
        self.assertFalse(detail["owned_by_me"])
        self.assertEqual(detail["local_group_id"], "")
        own_detail = self.run_as(self.alice, app_module.chat_attachment_detail(message["attachment"]["id"]))
        self.assertTrue(own_detail["owned_by_me"])
        self.assertEqual(own_detail["local_group_id"], group_id)

    def test_minimax_h3_history_task_can_be_sent_streamed_and_imported(self) -> None:
        conversation_id = self.befriend()
        group_id, _, _ = self.insert_h3_group()
        message = self.run_as(self.alice, app_module.send_chat_message(conversation_id, {
            "attachment": {"kind": "task", "group_id": group_id},
        }))
        attachment = message["attachment"]
        self.assertEqual(attachment["workflow"], "minimax-h3")
        self.assertEqual(attachment["video_count"], 1)
        self.assertEqual(attachment["media_type"], "video/mp4")
        self.assertTrue(attachment["primary_url"])
        self.assertEqual(
            attachment["preview_url"],
            f"/api/chat/attachments/{attachment['id']}/files/final-0/poster?preview=512",
        )

        detail = self.run_as(self.bob, app_module.chat_attachment_detail(attachment["id"]))
        final = detail["items"][0]["final"]
        self.assertEqual(final["media_type"], "video/mp4")
        self.assertEqual(
            final["preview_url"],
            f"/api/chat/attachments/{attachment['id']}/files/final-0/poster?preview=512",
        )
        response = self.run_as(self.bob, app_module.chat_attachment_file(attachment["id"], final["id"]))
        self.assertEqual(response.media_type, "video/mp4")
        self.assertEqual(response.headers["accept-ranges"], "bytes")

        imported = self.run_as(self.bob, app_module.import_chat_attachment(attachment["id"]))
        imported_final = imported["job"]["items"][0]["final"]
        self.assertEqual(imported_final["media_type"], "video/mp4")
        self.assertTrue(str(imported_final["filename"]).endswith(".mp4"))

    def test_local_image_media_is_private_quota_counted_and_editable(self) -> None:
        conversation_id = self.befriend()
        buffer = BytesIO()
        Image.new("RGB", (24, 18), "blue").save(buffer, format="PNG")
        content = buffer.getvalue()
        upload = UploadFile(
            file=BytesIO(content), filename="example.png",
            headers=Headers({"content-type": "image/png"}),
        )
        message = self.run_as(self.alice, app_module.send_chat_media(
            conversation_id, [upload], "look at this", "media-one",
        ))
        attachment = message["attachment"]
        self.assertEqual(attachment["kind"], "media")
        self.assertEqual(attachment["image_count"], 1)
        self.assertEqual((attachment["files"][0]["width"], attachment["files"][0]["height"]), (24, 18))
        self.assertGreaterEqual(app_module.user_storage_bytes(self.alice["user_id"]), len(content))
        detail = self.run_as(self.bob, app_module.chat_attachment_detail(attachment["id"]))
        self.assertEqual(detail["files"][0]["name"], "example.png")
        self.assertEqual((detail["files"][0]["width"], detail["files"][0]["height"]), (24, 18))
        prepared = self.run_as(self.bob, app_module.prepare_chat_attachment_edit(
            attachment["id"], {"file_id": "media-1"},
        ))
        self.assertTrue(prepared["token"])
        charlie = app_module.create_user_account("Charlie", "charlie-password-123")
        with self.assertRaises(HTTPException) as raised:
            self.run_as(charlie, app_module.chat_attachment_detail(attachment["id"]))
        self.assertEqual(raised.exception.status_code, 404)

    def test_chat_accepts_animated_gif_but_does_not_offer_it_for_editing(self) -> None:
        conversation_id = self.befriend()
        buffer = BytesIO()
        frames = [Image.new("RGB", (12, 10), color) for color in ("red", "blue")]
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
        upload = UploadFile(
            file=BytesIO(buffer.getvalue()), filename="animated.gif",
            headers=Headers({"content-type": "image/gif"}),
        )
        message = self.run_as(self.alice, app_module.send_chat_media(
            conversation_id, [upload], "animated", "gif-one",
        ))
        attachment = message["attachment"]
        detail = self.run_as(self.bob, app_module.chat_attachment_detail(attachment["id"]))
        self.assertEqual(detail["files"][0]["media_type"], "image/gif")
        response = self.run_as(self.bob, app_module.chat_attachment_file(attachment["id"], "media-1"))
        self.assertEqual(response.media_type, "image/gif")
        with self.assertRaises(HTTPException) as raised:
            self.run_as(self.bob, app_module.prepare_chat_attachment_edit(
                attachment["id"], {"file_id": "media-1"},
            ))
        self.assertEqual(raised.exception.status_code, 415)

    def test_chat_rejects_damaged_gif(self) -> None:
        conversation_id = self.befriend()
        upload = UploadFile(
            file=BytesIO(b"GIF89a-not-a-real-image"), filename="broken.gif",
            headers=Headers({"content-type": "image/gif"}),
        )
        with self.assertRaises(HTTPException) as raised:
            self.run_as(self.alice, app_module.send_chat_media(
                conversation_id, [upload], "", "gif-broken",
            ))
        self.assertEqual(raised.exception.status_code, 415)

    def test_local_media_rejects_mixed_files_and_video_is_streamable(self) -> None:
        conversation_id = self.befriend()
        image_buffer = BytesIO()
        Image.new("RGB", (8, 8), "green").save(image_buffer, format="PNG")
        image = UploadFile(file=BytesIO(image_buffer.getvalue()), filename="a.png", headers=Headers({"content-type": "image/png"}))
        video_bytes = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        video = UploadFile(file=BytesIO(video_bytes), filename="clip.mp4", headers=Headers({"content-type": "video/mp4"}))
        with self.assertRaises(HTTPException) as raised:
            self.run_as(self.alice, app_module.send_chat_media(conversation_id, [image, video], "", "mixed"))
        self.assertEqual(raised.exception.status_code, 415)
        video = UploadFile(file=BytesIO(video_bytes), filename="clip.mp4", headers=Headers({"content-type": "video/mp4"}))
        compatible = {"video_info": {"codec": "h264", "pix_fmt": "yuv420p", "compatible": True}, "playback_status": "original"}
        with patch.object(app_module, "prepare_chat_video", return_value=compatible):
            message = self.run_as(self.alice, app_module.send_chat_media(conversation_id, [video], "", "video-one"))
        response = self.run_as(self.bob, app_module.chat_attachment_file(message["attachment"]["id"], "media-1"))
        self.assertEqual(response.media_type, "video/mp4")
        self.assertEqual(response.headers["accept-ranges"], "bytes")

    def test_incompatible_video_keeps_original_and_serves_compatible_derivative(self) -> None:
        conversation_id = self.befriend()
        video_bytes = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        video = UploadFile(file=BytesIO(video_bytes), filename="hdr.mov", headers=Headers({"content-type": "video/quicktime"}))

        def converted(path: Path, relative_folder: Path) -> dict:
            playback = path.with_name(f"{path.stem}.playback.mp4")
            playback.write_bytes(b"browser-compatible")
            return {
                "video_info": {"codec": "hevc", "pix_fmt": "yuv420p10le", "compatible": False},
                "playback_status": "converted", "playback_media_type": "video/mp4",
                "playback_size": playback.stat().st_size,
                "playback": {"filename": playback.name, "subfolder": relative_folder.as_posix(), "type": "chat"},
            }

        with patch.object(app_module, "prepare_chat_video", side_effect=converted):
            message = self.run_as(self.alice, app_module.send_chat_media(conversation_id, [video], "", "video-hdr"))
        detail = self.run_as(self.bob, app_module.chat_attachment_detail(message["attachment"]["id"]))
        shared = detail["files"][0]
        self.assertEqual(shared["media_type"], "video/mp4")
        self.assertEqual(shared["original_media_type"], "video/quicktime")
        self.assertEqual(shared["playback_status"], "converted")
        playback = self.run_as(self.bob, app_module.chat_attachment_file(message["attachment"]["id"], "media-1"))
        original = self.run_as(self.bob, app_module.chat_attachment_file(message["attachment"]["id"], "media-1", download=True))
        self.assertTrue(str(playback.path).endswith(".playback.mp4"))
        self.assertTrue(str(original.path).endswith(".mov"))

    def test_shared_image_can_be_prepared_but_expires_with_sender_file(self) -> None:
        conversation_id = self.befriend()
        group_id, item_id, path = self.insert_image_group()
        message = self.run_as(self.alice, app_module.send_chat_message(conversation_id, {
            "attachment": {"kind": "image", "group_id": group_id, "item_id": item_id, "source_kind": "edit"},
        }))
        prepared = self.run_as(self.bob, app_module.prepare_chat_attachment_edit(message["attachment"]["id"], {}))
        token = app_module.CURRENT_USER.set(self.bob)
        try:
            self.assertTrue(app_module.safe_source_path(app_module.prepared_chat_asset(prepared["token"])).is_file())
        finally:
            app_module.CURRENT_USER.reset(token)
        path.unlink()
        attachment = app_module.chat_attachment_record(message["attachment"]["id"], self.bob["user_id"])
        self.assertFalse(app_module.chat_attachment_public(attachment)["available"])


if __name__ == "__main__":
    unittest.main()
