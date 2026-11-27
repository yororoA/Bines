"""Real runner/gateway permission paths with a fake OneBot peer, no model or QQ."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]


class StickerPermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bines-sticker-permissions-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(root / "memory"),
            "BINES_GATEWAY_DIR": str(root / "gateway"), "BINES_HEALTH_DIR": str(root / "health"),
            "BINES_MODEL_USAGE_PATH": str(root / "usage.db"), "BINES_WEBSITE_BRIDGE_SPOOL": str(root / "website"),
            "BINES_ACTIVITY_QUANTUM_ACTIONS": "24", "BINES_ACTIVITY_QUANTUM_SECONDS": "90",
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)
        from chatBot.capabilities import CapabilityService, SchemaCatalog
        from chatBot.gateway import GatewayServer
        from chatBot.gateway_client import GatewayClient
        from chatBot.messages import Identity, LOCAL_IDENTITY
        from chatBot.store import MemoryStore
        from skill_runner import SkillRunner
        from tools import napcat_tool, qq_tool
        from tools.context import ToolContext, tool_context
        from will_runtime import WillSkillRunner
        self.Context, self.context = ToolContext, tool_context
        self.Wrapper, self.local = WillSkillRunner, LOCAL_IDENTITY
        self.owner = Identity("99", "1", owner=True)
        self.store = MemoryStore(root / "gateway.db", owners={"1"})
        event = {"self_id": 99, "post_type": "message", "message_type": "private",
                 "user_id": "1", "message_id": 1, "time": time.time(),
                 "message": [{"type": "text", "data": {"text": "fixture"}}],
                 "sender": {"user_id": "1", "nickname": "fixture"}}
        self.store.ingest(event)
        with self.store.connection() as db:
            self.event_id = db.execute("SELECT id FROM events").fetchone()[0]
        self.api = Mock()
        self.api.raw_api.side_effect = self.fake_api
        self.api.is_connected = True
        self.send_status = "ok"
        if (ROOT / "chatBot" / "stickers.py").exists():
            media = patch("chatBot.stickers.shared_media_dir", return_value=root / "media")
            media.start()
            self.addCleanup(media.stop)
        service = CapabilityService(self.api, self.store, SchemaCatalog(token=""))
        self.server = GatewayServer(service, ("127.0.0.1", 0), token="offline-test")
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = GatewayClient(f"http://127.0.0.1:{self.server.server_port}", token="offline-test")
        for target in ("tools.napcat_tool.GatewayClient", "tools.qq_tool.get_qq_client"):
            patcher = patch(target, return_value=self.client)
            patcher.start()
            self.addCleanup(patcher.stop)

        def send_sticker(sticker_id: str, user_id: str = "", group_id: str = ""):
            """Fixture for the new tool's canonical raw_api dispatch contract."""
            return napcat_tool.napcat_send(
                [{"type": "image", "data": {"file": "base64://R0lG", "sub_type": 1}}],
                user_id=user_id, group_id=group_id)

        self.collect = Mock(return_value={"status": "ok", "sticker_id": "fixture"})
        self.search = Mock(return_value=[])

        def collect_sticker():
            return self.collect()

        def search_stickers():
            return self.search()

        registry = {"napcat_send": napcat_tool.napcat_send,
                    "napcat_forward": napcat_tool.napcat_forward,
                    "napcat_send_custom_face": napcat_tool.napcat_send_custom_face,
                    "napcat_send_sticker": send_sticker,
                    "napcat_call": napcat_tool.napcat_call,
                    "send_qq_private_msg": qq_tool.send_qq_private_msg,
                    "send_qq_group_msg": qq_tool.send_qq_group_msg,
                    "napcat_collect_sticker": collect_sticker,
                    "napcat_search_stickers": search_stickers}
        skills = root / "skills" / "napcat"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("---\nname: napcat\ndescription: Fixture tools\nactions: "
                                         + ", ".join(registry) + "\n---\nFixture only.\n")
        # The fixture tests dispatch/transport authority, not production action
        # registration (owned by the sticker feature integration).
        self.runner = SkillRunner(registry, skills_dir=skills.parent)

    def fake_api(self, action, params, timeout=30):
        if action == "fetch_custom_face_detail":
            return {"status": "ok", "data": [{"url": "https://example.invalid/fixture.gif", "desc": "fixture"}]}
        return {"status": self.send_status, "retcode": 0, "data": {"message_id": 101}}

    def command(self, name, *, target="1", group=False):
        destination = {"group_id" if group else "user_id": target}
        args = {**destination, "message": "fixture"}
        if name == "napcat_send_sticker":
            args = {**destination, "sticker_id": "fixture"}
        elif name == "napcat_send_custom_face":
            args = {**destination, "index": 0}
        elif name == "napcat_forward":
            args = {**destination, "message_ids": ["1"]}
        elif name == "napcat_call":
            args = {"action": "send_group_msg" if group else "send_private_msg", "params": args}
        return {"skill": "napcat", "action": name, "args": args}

    @staticmethod
    def sends():
        return ("napcat_send", "napcat_forward", "napcat_send_custom_face", "napcat_send_sticker",
                "send_qq_private_msg", "napcat_call")

    def test_ordinary_owner_and_full_local_allow_all_send_routes_without_candidate_flags(self):
        for local in (False, True):
            for name in self.sends():
                with self.subTest(local=local, route=name):
                    context = self.Context(self.local if local else self.owner,
                        "desktop:fixture" if local else self.event_id,
                        f"route:{local}:{name}")
                    before = self.api.raw_api.call_count
                    with self.context(context):
                        result = self.Wrapper(self.runner, {}, lambda: "", ordinary=True).execute(self.command(name))
                    self.assertEqual(json.loads(result)["status"], "ok")
                    self.assertGreater(self.api.raw_api.call_count, before)
                    self.assertTrue(context.receipts[0]["operation_id"].startswith(context.turn_id + ":"))

    def test_ordinary_group_send_routes_are_not_mistaken_for_autonomous_contact(self):
        for name in ("napcat_send", "napcat_send_sticker", "send_qq_group_msg", "napcat_call"):
            context = self.Context(self.owner, self.event_id, "group:" + name)
            with self.subTest(route=name), self.context(context):
                self.Wrapper(self.runner, {}, lambda: "", ordinary=True).execute(
                    self.command(name, target="10", group=True))
                self.assertEqual(context.receipts[-1]["result"]["status"], "ok")

    def test_autonomous_missing_or_false_contact_flags_block_wrappers_and_generic_writes(self):
        for candidate in ({}, {"contact_allowed": False}):
            for name in self.sends():
                with self.subTest(candidate=candidate, route=name), self.context(
                        self.Context(self.local, autonomous=True, contact_allowed=True)):
                    result = self.Wrapper(self.runner, candidate, lambda: "").execute(self.command(name))
                    self.assertIn("未执行", result)
        self.api.raw_api.assert_not_called()

    def test_ordinary_explicit_candidate_or_context_denial_is_preserved(self):
        for candidate, allowed in (({"contact_allowed": False}, True), ({}, False)):
            with self.subTest(candidate=candidate), self.context(self.Context(self.local, contact_allowed=allowed)):
                wrapper = self.Wrapper(self.runner, candidate, lambda: "", ordinary=True)
                for name in self.sends():
                    self.assertIn("未执行", wrapper.execute(self.command(name)))
        self.api.raw_api.assert_not_called()

    def test_quiet_autonomous_can_collect_and_search_but_not_write_raw_api(self):
        with self.context(self.Context(self.local, autonomous=True, contact_allowed=False)):
            wrapper = self.Wrapper(self.runner, {"contact_allowed": False}, lambda: "")
            for name in ("napcat_collect_sticker", "napcat_search_stickers"):
                self.assertNotIn("未执行", wrapper.execute({"skill": "napcat", "action": name, "args": {}}))
            read = {"skill": "napcat", "action": "napcat_call", "args": {"action": "get_status", "params": {}}}
            self.assertEqual(json.loads(wrapper.execute(read))["status"], "ok")
            with self.assertRaises(PermissionError):
                self.client.raw_api("set_group_card", {"group_id": "10"})
        self.collect.assert_called_once()
        self.search.assert_called_once()
        self.api.raw_api.assert_called_once()

    def test_planning_only_blocks_collection_search_and_sends_from_either_policy_source(self):
        for candidate_plan, context_plan in ((True, False), (False, True)):
            with self.subTest(candidate_plan=candidate_plan), self.context(
                    self.Context(self.local, autonomous=True, planning_only=context_plan)):
                wrapper = self.Wrapper(self.runner, {"planning_only": candidate_plan, "contact_allowed": True}, lambda: "")
                self.assertEqual(wrapper.function_tools(), [])
                for name in (*self.sends(), "napcat_collect_sticker", "napcat_search_stickers"):
                    self.assertIn("未执行", wrapper.execute(self.command(name)))
        self.collect.assert_not_called()
        self.search.assert_not_called()
        self.api.raw_api.assert_not_called()

    def test_owner_cooldown_is_checked_after_alias_destination_resolution(self):
        for name in ("napcat_send", "napcat_send_sticker", "napcat_call"):
            with self.subTest(route=name), self.context(self.Context(self.local,
                    turn_id="cooldown:" + name, autonomous=True, owner_contact_allowed=False,
                    owner_ids=frozenset({"1"}))):
                wrapper = self.Wrapper(self.runner, {"contact_allowed": True}, lambda: "")
                before = self.api.raw_api.call_count
                self.assertIn("主人私聊", wrapper.execute(self.command(name)))
                self.assertEqual(self.api.raw_api.call_count, before)
                for target, group in (("2", False), ("10", True)):
                    result = wrapper.execute(self.command(name, target=target, group=group))
                    self.assertEqual(json.loads(result)["status"], "ok")

    def test_lost_lease_after_preparation_prevents_sticker_dispatch(self):
        check = Mock(side_effect=["", "lease_lost"])
        with self.context(self.Context(self.local, turn_id="lease", check=check)):
            wrapper = self.Wrapper(self.runner, {}, lambda: "", ordinary=True)
            with self.assertRaisesRegex(RuntimeError, "lease_lost"):
                wrapper.execute(self.command("napcat_send_sticker"))
        self.api.raw_api.assert_not_called()

    def test_uncertain_sticker_receipt_blocks_a_different_send_route(self):
        from will_runtime import ActivityYield
        self.send_status = "uncertain"
        context = self.Context(self.local, event_id="desktop:uncertain", turn_id="desktop:uncertain")
        with self.context(context):
            wrapper = self.Wrapper(self.runner, {}, lambda: "", ordinary=True)
            with self.assertRaises(ActivityYield):
                wrapper.execute(self.command("napcat_send_sticker"))
            with self.assertRaisesRegex(RuntimeError, "不确定写操作"):
                self.client.raw_api("send_private_msg", {"user_id": "1", "message": "retry"})
        self.api.raw_api.assert_called_once()
        self.assertEqual(context.activity["mode"], "pause")

    def test_full_local_keeps_turn_receipts_without_forwarding_desktop_event_id(self):
        with self.context(self.Context(self.local, "desktop:fixture", "desktop:fixture")):
            params = {"user_id": "1", "message": "fixture"}
            first = self.client.raw_api("send_private_msg", params, operation_id="same")
            second = self.client.raw_api("send_private_msg", params, operation_id="same")
        self.assertEqual(first, second)
        self.assertEqual(first["operation_id"], "desktop:fixture:tool:same")
        self.api.raw_api.assert_called_once()

    def test_explicit_qq_lookup_is_preserved_even_from_local_context(self):
        with self.context(self.Context(self.local, "desktop:fixture", "desktop:fixture")):
            result = self.client.request("/health", {}, event_id=self.event_id)
            self.assertTrue(result["connected"])
            with self.assertRaisesRegex(RuntimeError, "未知 QQ 事件"):
                self.client.request("/health", {}, event_id="missing-qq-event")

    def test_remote_missing_or_unknown_event_never_falls_back_to_local_authority(self):
        for event_id in (None, "missing-qq-event"):
            with self.subTest(event_id=event_id), self.context(self.Context(self.owner, event_id, "remote")):
                with self.assertRaises((PermissionError, RuntimeError)):
                    self.client.raw_api("send_private_msg", {"user_id": "2", "message": "fixture"})
        self.api.raw_api.assert_not_called()

    def test_body_event_id_cannot_override_the_trusted_context(self):
        with self.context(self.Context(self.owner, self.event_id, "remote")):
            result = self.client.request("/health", {"event_id": "forged"})
        self.assertTrue(result["connected"])


if __name__ == "__main__":
    unittest.main()
