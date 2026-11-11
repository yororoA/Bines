"""Real local HTTP + tool dispatch + disk; the only QQ transport is a fake."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import helpers  # noqa: F401
from PIL import Image
from chatBot.capabilities import CapabilityService
from chatBot.gateway import GatewayServer
from chatBot.gateway_client import GatewayClient
from chatBot.messages import Identity, LOCAL_IDENTITY
from chatBot.stickers import StickerLibrary
from chatBot.store import MemoryStore
from skill_runner import SkillRunner, QQ_ALLOWED_ACTIONS
from tools import TOOLS_REGISTRY
from tools.context import ToolContext, tool_context
from tools.napcat_tool import napcat_collect_sticker, napcat_search_stickers, napcat_send_sticker, napcat_operation
from will_runtime import WillSkillRunner


class StickerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = MemoryStore(root / "gateway.db", owners={"1"})
        image = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(image, format="GIF", save_all=True,
                                             append_images=[Image.new("RGB", (2, 2), "blue")], duration=100, loop=0)
        self.binary = image.getvalue()
        self.library = StickerLibrary(self.store, media_dir=root / "media", downloader=lambda *_a, **_kw: self.binary)
        self.calls, self.favorites = [], []
        self.add_status = "ok"
        self.messages = {}
        fixture = self

        class FakeNapCat:
            is_connected = True

            def raw_api(self, action, params, timeout=30):
                fixture.calls.append((action, params))
                if action == "get_msg":
                    return {"status": "ok", "retcode": 0, "data": fixture.messages[str(params["message_id"])]}
                if action == "fetch_custom_face_detail":
                    return {"status": "ok", "retcode": 0, "data": fixture.favorites}
                if action == "add_custom_face":
                    if fixture.add_status == "ok":
                        fixture.favorites.append({"md5": hashlib.md5(fixture.binary).hexdigest(),
                                                  "url": "https://p.qpic.cn/qq_expression/test.gif"})
                    return {"status": fixture.add_status, "retcode": 0}
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1000}}

        self.service = CapabilityService(FakeNapCat(), self.store)
        self.server = GatewayServer(self.service, ("127.0.0.1", 0), token="isolated", sticker_library=self.library)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.client = GatewayClient(f"http://127.0.0.1:{self.server.server_port}", token="isolated")
        patcher = patch("tools.napcat_tool.GatewayClient", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = SkillRunner(TOOLS_REGISTRY, skills_dir=Path(__file__).resolve().parents[2] / "skills",
                                  qq_allowed_actions=QQ_ALLOWED_ACTIONS)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def context(self, user="1", group="", message_id="10"):
        event = {"self_id": "99", "user_id": user, "post_type": "message", "message_id": message_id,
                 "message_type": "group" if group else "private", "sender": {"user_id": user},
                 "message": [{"type": "image", "data": {"url": "https://p.qpic.cn/test.gif", "file": "test.gif"}}]}
        if group:
            event["group_id"] = group
        self.store.ingest(event)
        self.messages[message_id] = event
        with self.store.connection() as db:
            event_id = db.execute("SELECT id FROM events ORDER BY received DESC LIMIT 1").fetchone()[0]
        return ToolContext(identity=self.store.identity(event), event_id=event_id, turn_id=event_id)

    def test_owner_collect_restart_search_and_send_via_ordinary_runner(self):
        context = self.context()
        with tool_context(context):
            guarded = WillSkillRunner(self.runner, {}, lambda: "", ordinary=True)
            result = guarded.execute({"skill": "napcat", "action": "napcat_collect_sticker", "args": {
                "description": "开心跳舞的猫", "tags": ["开心", "猫"], "operation_id": "save"}}, source="QQ")
            self.assertIn("confirmed", str(result))
            self.server.stickers.library = StickerLibrary(self.store, media_dir=Path(self.temp.name) / "media")
            rows = napcat_search_stickers("开心")
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["animated"])
            for secret in ("https://", "base64", "rkey", "file"):
                self.assertNotIn(secret, json.dumps(rows))
            result = guarded.execute({"skill": "napcat", "action": "napcat_send_sticker", "args": {
                "sticker_id": rows[0]["id"], "text": "好耶", "operation_id": "send"}}, source="QQ")
            self.assertIn('"status": "ok"', str(result))
        sends = [params for action, params in self.calls if action == "send_private_msg"]
        self.assertEqual(len(sends), 1)
        file = sends[0]["message"][1]["data"]["file"]
        self.assertTrue(file.startswith("/bines-media/stickers/"))
        self.assertEqual((Path(self.temp.name) / "media" / file.removeprefix("/bines-media/")).read_bytes(), self.binary)

    def test_ordinary_group_collection_and_sending_keep_scope(self):
        context = self.context("2", "10")
        with tool_context(context):
            saved = napcat_collect_sticker("开心", ["开心"])
            self.assertTrue(saved["local_saved"])
            self.assertEqual(saved["sticker"]["qq_sync"], "local_only")
            sticker_id = saved["sticker"]["id"]
            self.assertEqual(napcat_send_sticker(sticker_id)["status"], "ok")
        self.assertFalse(any(action == "add_custom_face" for action, _ in self.calls))
        with tool_context(self.context("1", "11", "11")):
            self.assertEqual(napcat_search_stickers("开心"), [])
            with self.assertRaises(RuntimeError):
                napcat_send_sticker(sticker_id)

    def test_full_desktop_import_and_send_preserve_turn_receipt(self):
        md5 = hashlib.md5(self.binary).hexdigest()
        self.favorites.append({"md5": md5, "url": "https://p.qpic.cn/test.gif"})
        context = ToolContext(event_id="desktop:synthetic", turn_id="desktop:synthetic")
        with tool_context(context):
            guarded = WillSkillRunner(self.runner, {}, lambda: "", ordinary=True)
            result = guarded.execute({"skill": "napcat", "action": "napcat_collect_sticker", "args": {
                "description": "笑", "favorite_md5": md5}})
            self.assertIn("confirmed", str(result))
            sticker_id = napcat_search_stickers("笑")[0]["id"]
            result = guarded.execute({"skill": "napcat", "action": "napcat_send_sticker", "args": {
                "sticker_id": sticker_id, "user_id": "1"}})
            self.assertIn('"status": "ok"', str(result))
        self.assertTrue(context.receipts[-1]["operation_id"].startswith("desktop:synthetic:tool:"))

    def test_duplicate_collect_and_uncertain_sync_never_repeat_account_write(self):
        self.add_status = "uncertain"
        context = self.context()
        params = {"description": "猫", "tags": ["猫"]}
        with tool_context(context):
            first = self.client.request("/stickers", {"method": "collect", "params": params,
                                        "operation_id": "isolated:collect"})
            self.assertTrue(first["local_saved"])
            self.assertEqual(first["status"], "uncertain")
            again = self.client.request("/stickers", {"method": "collect", "params": params,
                                        "operation_id": "isolated:collect"})
            self.assertEqual(first, again)
            quiet = self.client.request("/stickers", {"method": "collect", "params": params,
                                        "operation_id": "isolated:quiet", "sync_qq": False})
            self.assertEqual(quiet["status"], "uncertain")
            self.assertEqual(quiet["sticker"]["qq_sync"], "uncertain")
            self.client.request("/stickers", {"method": "collect", "params": params,
                                "operation_id": "isolated:new-id"})
        self.assertEqual(sum(action == "add_custom_face" for action, _ in self.calls), 1)

    def test_tool_catalog_is_fixed_and_available_in_both_modes(self):
        context = self.context("2", "10")
        with tool_context(context):
            before = self.runner.function_tools(source="QQ")
            rules = self.runner.instructions_for_source("QQ", native=True)
            for index in range(12):
                self.library.save(context.identity, {"type": "face", "data": {"id": str(index)}}, "笑" + str(index), ["开心"])
            # A large persistent collection must not expand fixed model instructions.
            with self.store.connection() as db:
                template = dict(db.execute("SELECT * FROM sticker_library LIMIT 1").fetchone())
                for index in range(12, 10000):
                    meta = json.loads(template["metadata"])
                    meta["id"] = "synthetic-volume-" + str(index)
                    db.execute("INSERT INTO sticker_library VALUES(?,?,?,?,?,?,?,?)", (
                        meta["id"], template["scope"], str(index), json.dumps(meta), template["segment"], "", 1, 1))
            after = self.runner.function_tools(source="QQ")
            self.assertEqual(before, after)
            self.assertEqual(rules, self.runner.instructions_for_source("QQ", native=True))
            self.assertLessEqual(len(napcat_search_stickers("开心", 5)), 5)
            self.assertLess(len(json.dumps(napcat_search_stickers("开心", 5), ensure_ascii=False)), 5000)
            names = {item["function"]["name"] for item in after}
            self.assertTrue({"napcat_collect_sticker", "napcat_search_stickers", "napcat_send_sticker"} <= names)

    def test_receipt_query_reconciles_late_success_without_writing_again(self):
        self.add_status = "uncertain"
        with tool_context(self.context()):
            result = napcat_collect_sticker("猫", ["猫"])
            self.assertEqual(result["status"], "uncertain")
            self.favorites.append({"md5": hashlib.md5(self.binary).hexdigest(), "url": "https://p.qpic.cn/test.gif"})
            receipt = napcat_operation(result["operation_id"])
            self.assertEqual(receipt["status"], "ok")
            self.assertEqual(receipt["sticker"]["qq_sync"], "confirmed")
            self.assertEqual(napcat_send_sticker(result["sticker"]["id"])["status"], "ok")
        self.assertEqual(sum(action == "add_custom_face" for action, _ in self.calls), 1)

    def test_owner_group_collection_does_not_read_or_write_private_favorites(self):
        with tool_context(self.context("1", "10")):
            result = napcat_collect_sticker("猫", ["猫"])
            self.assertEqual(result["sticker"]["qq_sync"], "local_only")
        self.assertFalse(any(action in {"add_custom_face", "fetch_custom_face_detail"} for action, _ in self.calls))

    def test_string_false_never_publishes_private_sticker(self):
        with tool_context(self.context()):
            with self.assertRaises(ValueError):
                napcat_collect_sticker("私聊图片", shared="false")
            result = self.runner.execute({"skill": "napcat", "action": "napcat_collect_sticker", "args": {
                "description": "私聊图片", "shared": "false"}}, source="QQ")
            self.assertIn("布尔值", result)
        self.assertEqual(self.library.search(Identity("99", "3", group="10")), [])

    def test_proactive_cancel_during_download_prevents_remote_favorite_write(self):
        self.context()
        event = {"self_id": "99", "user_id": "1", "post_type": "bines_proactive", "proactive": {"contact_allowed": True}}
        self.store.ingest(event)
        with self.store.connection() as db:
            event_id = db.execute("SELECT id FROM events ORDER BY received DESC LIMIT 1").fetchone()[0]
        cancelled = []
        def download(*_a, **_kw):
            cancelled.append(True)
            return self.binary
        self.library.downloader = download
        authority = ToolContext(identity=self.store.identity(event), event_id=event_id, turn_id=event_id,
                                autonomous=True, contact_allowed=True)
        with tool_context(authority), patch("chatBot.proactive.guard", side_effect=lambda *_a, **_kw:
                                            "incoming_message" if cancelled else ""), \
                patch("chatBot.proactive.dispatch_allowed"):
            result = napcat_collect_sticker("猫", message_id="10")
        self.assertTrue(result["local_saved"])
        self.assertEqual(result["sticker"]["qq_sync"], "local_only")
        self.assertFalse(any(action == "add_custom_face" for action, _ in self.calls))

    def test_source_recalled_during_download_is_never_saved_or_synced(self):
        context = self.context()
        def download(*_a, **_kw):
            with self.store.connection() as db:
                db.execute("UPDATE messages SET recalled=1,raw=NULL")
            return self.binary
        self.library.downloader = download
        with tool_context(context):
            result = napcat_collect_sticker("猫")
            self.assertFalse(result["local_saved"])
            self.assertEqual(napcat_search_stickers(), [])
        self.assertEqual(list((Path(self.temp.name) / "media" / "stickers").iterdir()), [])
        self.assertFalse(any(action == "add_custom_face" for action, _ in self.calls))

    def test_owner_group_generic_send_cannot_bypass_private_library_scope(self):
        owner = self.context()
        with tool_context(owner):
            sticker_id = napcat_collect_sticker("私聊猫")["sticker"]["id"]
        segment = self.library.resolve(owner.identity, sticker_id)
        with tool_context(self.context("1", "10", "11")):
            for message in ([segment], [{"type": "image", "data": {
                    "file": "file://" + segment["data"]["file"]}}], [{"type": "node", "data": {
                    "user_id": "99", "nickname": "fixture", "content": [segment]}}],
                    [{"type": "image", "data": {"file": "/" + segment["data"]["file"]}}],
                    [{"type": "image", "data": {"file": "file:///" + segment["data"]["file"]}}]):
                with self.assertRaisesRegex(RuntimeError, "表情文件"):
                    self.client.raw_api("send_group_msg", {"group_id": "10", "message": message})
            with self.assertRaisesRegex(RuntimeError, "不能同时"):
                self.client.raw_api("send_group_msg", {"group_id": "10", "message": [segment],
                                    "messages": [{"type": "text", "data": {"text": "mask"}}]})
        self.assertFalse(any(action == "send_group_msg" for action, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
