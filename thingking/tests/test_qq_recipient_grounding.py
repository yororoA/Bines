"""Autonomous recipient provenance through real local HTTP, with fake QQ only."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]

from chatBot.capabilities import CapabilityService
from chatBot.gateway import GatewayServer
from chatBot.gateway_client import GatewayClient
from chatBot.messages import Identity, LOCAL_IDENTITY, SEND_ACTIONS
from chatBot.recipients import RecipientRegistry, send_action
from chatBot.store import MemoryStore
from common.memory_journal import MemoryOutbox
from tools.context import ToolContext, tool_context


def incoming(user="1", mid=1, *, account="99", group="", post_type="message"):
    return {"post_type": post_type, "self_id": account, "user_id": user,
            "message_id": mid, "message_type": "group" if group else "private", "group_id": group,
            "message": [{"type": "text", "data": {"text": "synthetic input"}}], "time": time.time() - 3600}


class RecipientGroundingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bines-recipient-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ENV_FILE": "/dev/null", "BINES_ENV_FILE": "/dev/null",
            "BINES_MEMORY_DIR": str(self.root / "memory"), "BINES_GATEWAY_DIR": str(self.root / "gateway"),
            "BINES_HEALTH_DIR": str(self.root / "health"), "BINES_MODEL_USAGE_PATH": str(self.root / "usage.db"),
            "BINES_WEBSITE_BRIDGE_SPOOL": str(self.root / "spool")})
        env.start()
        self.addCleanup(env.stop)
        self.store = MemoryStore(self.root / "gateway.db", owners={"1"})
        self.store.ingest(incoming())
        from common.proactive import human_ref
        with self.store.connection() as db:
            row = dict(db.execute("SELECT * FROM messages").fetchone())
            db.execute("UPDATE events SET status='done'")
        self.owner = Identity("99", "1", owner=True)
        self.candidate = {"kind": "initiative", "topic": "synthetic topic", "intent": "synthetic intent", "reason": "synthetic reason",
                          "timestamp": time.time(), "based_on_user": human_ref(row), "activity_at": time.time(),
                          "contact_allowed": True, "owner_contact_allowed": True}
        ev = {**incoming(mid=2), "post_type": "bines_proactive", "proactive": self.candidate}
        self.store.ingest(ev)
        with self.store.connection() as db:
            self.event_id = db.execute("SELECT id FROM events WHERE json_extract(raw,'$.post_type')='bines_proactive'").fetchone()[0]
            db.execute("UPDATE events SET status='working',worker='test',lease=? WHERE id=?", (time.time()+120, self.event_id))
            db.execute("INSERT INTO proactive_runs(id,conversation,candidate) VALUES(?,?,?)", (
                self.event_id, self.owner.conversation, json.dumps(self.candidate)))
        self.calls, self.responses = [], {}
        fixture = self

        class API:
            is_connected = True
            def raw_api(self, action, params, timeout=30):
                fixture.calls.append((action, copy.deepcopy(params)))
                return copy.deepcopy(fixture.responses.get(action, {"status": "ok", "retcode": 0,
                                                                   "data": {"message_id": 900}}))

        self.service = CapabilityService(API(), self.store, catalog=object())
        hub = SimpleNamespace(outbox=MemoryOutbox(self.store.path),
                              call=lambda *_a, **_kw: {"last_owner_activity": {"ts": 0}},
                              remember_receipt=lambda *_a, **_kw: None)
        for target, value in [("chatBot.stickers.shared_media_dir", lambda: self.root / "media"),
                              ("common.activity.activity_control", lambda: {}),
                              ("common.proactive.ProactiveConfig.quiet", lambda *_: False)]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.server = GatewayServer(self.service, ("127.0.0.1", 0), token="offline", memory_hub=hub)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.client = GatewayClient(f"http://127.0.0.1:{self.server.server_port}", token="offline")
        p = patch("tools.napcat_tool.GatewayClient", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)
        self.serial = 0

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, action, params, *, operation_id=None, event_id=None):
        self.serial += 1
        op = operation_id or self.event_id + ":test:" + str(self.serial)
        return self.client.request("/action", {"action": action, "params": params,
            "operation_id": op if send_action(action) else None}, event_id=event_id or self.event_id)

    def writes(self):
        return [row for row in self.calls if send_action(row[0])]

    def add_inbound(self, *args, **kwargs):
        self.store.ingest(incoming(*args, **kwargs))
        with self.store.connection() as db:
            db.execute("UPDATE events SET status='done' WHERE status='pending'")

    def test_default_current_conversation_sends_without_model_supplied_id(self):
        from tools.napcat_tool import napcat_send
        with tool_context(ToolContext(self.owner, self.event_id, self.event_id, autonomous=True)):
            self.assertEqual(napcat_send("synthetic reply")["status"], "ok")
        self.assertEqual(self.writes()[0][1]["user_id"], "1")

    def test_unknown_explicit_id_is_rejected_before_operation_and_journal_then_default_can_send(self):
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.request("send_private_msg", {"user_id": "7777777777", "message": "synthetic reply"})
        self.assertEqual(self.writes(), [])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0], 0)
        self.assertEqual(self.request("send_private_msg", {"user_id": "1", "message": "synthetic reply"})["status"], "ok")

    def test_real_same_account_inbound_user_and_group_allow_cross_target(self):
        self.add_inbound("2", 20, group="10")
        for action, params in [("send_private_msg", {"user_id": "2"}), ("send_group_msg", {"group_id": "10"})]:
            with self.subTest(action=action):
                self.assertEqual(self.request(action, {**params, "message": "synthetic"})["status"], "ok")
        self.assertEqual(len(self.writes()), 2)

    def test_other_account_internal_self_and_withdrawn_events_do_not_grant_target(self):
        self.add_inbound("2", 20, account="98")
        self.add_inbound("3", 30, post_type="bines_proactive")
        self.add_inbound("4", 40, post_type="message_sent")
        self.add_inbound("99", 50, group="11")
        self.add_inbound("5", 60)
        with self.store.connection() as db:
            db.execute("UPDATE messages SET recalled=1 WHERE external_id='99:p:5:60'")
        for user in ("2", "3", "4", "99", "5"):
            with self.subTest(user=user), self.assertRaisesRegex(RuntimeError, "没有可信来源"):
                self.request("send_private_msg", {"user_id": user, "message": "synthetic"})
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.request("send_group_msg", {"group_id": "11", "message": "synthetic"})

    def test_message_text_and_message_id_are_never_recipient_proofs(self):
        ev = incoming("2", 3333333333)
        ev["message"][0]["data"]["text"] = "7777777777"
        self.store.ingest(ev)
        for user in ("3333333333", "7777777777"):
            with self.subTest(user=user), self.assertRaisesRegex(RuntimeError, "没有可信来源"):
                self.request("send_private_msg", {"user_id": user, "message": "synthetic"})

    def test_successful_real_friend_group_and_member_lists_grant_only_response_ids(self):
        cases = [("get_friend_list", [{"user_id": "2"}], "send_private_msg", {"user_id": "2"}),
                 ("get_group_list", [{"group_id": "10"}], "send_group_msg", {"group_id": "10"}),
                 ("get_group_member_list", [{"user_id": "3", "group_id": "11"}], "send_private_msg", {"user_id": "3"})]
        for read, data, send, target in cases:
            with self.subTest(read=read):
                self.responses[read] = {"status": "ok", "retcode": 0, "data": data}
                self.request(read, {"group_id": "7777777777", "user_id": "8888888888"})
                self.assertEqual(self.request(send, {**target, "message": "synthetic"})["status"], "ok")
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.request("send_group_msg", {"group_id": "7777777777", "message": "synthetic"})
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.request("send_private_msg", {"user_id": "8888888888", "message": "synthetic"})

    def test_failed_malformed_empty_or_foreign_account_lists_do_not_grant(self):
        bad = [{"status": "failed", "retcode": 0, "data": [{"user_id": "2"}]},
               {"status": "ok", "retcode": 1200, "data": [{"user_id": "2"}]},
               {"status": "ok", "retcode": False, "data": [{"user_id": "2"}]},
               {"status": "ok", "retcode": 0, "data": {"user_id": "2"}},
               {"status": "ok", "retcode": 0, "data": []},
               {"status": "ok", "retcode": 0, "data": [{"user_id": True}, {"nickname": "2"}]}]
        for result in bad:
            self.responses["get_friend_list"] = result
            self.request("get_friend_list", {"user_id": "2"})
        self.service.recipients.observe(Identity("98", "1", owner=True), "get_friend_list", {},
                                        {"status": "ok", "retcode": 0, "data": [{"user_id": "2"}]})
        self.service.recipients.observe(LOCAL_IDENTITY, "get_friend_list", {},
                                        {"status": "ok", "retcode": 0, "data": [{"user_id": "2"}]})
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.request("send_private_msg", {"user_id": "2", "message": "synthetic"})

    def test_all_six_send_routes_and_forward_message_type_overrides_are_checked(self):
        for action in SEND_ACTIONS:
            private = "private" in action or action in {"send_msg", "send_forward_msg"}
            target = {"user_id": "7777777777"} if private else {"group_id": "7777777777"}
            field = "messages" if "forward" in action else "message"
            chain = [{"type": "node", "data": {"user_id": "1", "nickname": "synthetic", "content": "synthetic"}}]
            params = {**target, field: chain if "forward" in action else "synthetic"}
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "没有可信来源"):
                self.request(action, params)
        # Forward action names do not override an explicit message_type in this NapCat build.
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.request("send_group_forward_msg", {"group_id": "10", "user_id": "7777777777",
                "message_type": "private", "messages": [{"type": "node", "data": {"content": "synthetic"}}]})
        self.add_inbound("2", 20, group="10")
        self.assertEqual(self.request("send_private_forward_msg", {"user_id": "7777777777", "group_id": "10",
            "message_type": "group", "messages": [{"type": "node", "data": {"content": "synthetic"}}]})["status"], "ok")

    def test_generic_actual_group_route_ignores_unused_user_but_private_fallback_needs_group_proof(self):
        self.add_inbound("2", 20, group="10")
        self.assertEqual(self.request("send_msg", {"group_id": "10", "user_id": "7777777777",
                                                  "message": "synthetic"})["status"], "ok")
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.request("send_private_msg", {"user_id": "1", "group_id": "7777777777", "message": "synthetic"})

    def test_autonomous_aliases_require_synchronous_send_even_for_known_targets(self):
        for base in SEND_ACTIONS:
            for suffix in ("_async", "_rate_limited"):
                action = base + suffix
                private = "private" in base or base in {"send_msg", "send_forward_msg"}
                params = {"user_id" if private else "group_id": "7777777777", "message": "synthetic"}
                with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "请使用同步动作"):
                    self.request(action, params)
        self.assertEqual(self.writes(), [])
        for suffix in ("_async", "_rate_limited"):
            with self.assertRaisesRegex(RuntimeError, "请使用同步动作"):
                self.request("send_private_msg" + suffix, {"user_id": "1", "message": "synthetic"})
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0], 0)
        self.responses["get_friend_list_async"] = {"status": "ok", "retcode": 0, "data": [{"user_id": "2"}]}
        self.request("get_friend_list_async", {})
        self.assertEqual(self.request("send_private_msg", {"user_id": "2", "message": "synthetic"})["status"], "ok")

    def test_restarted_registry_cannot_reuse_unpersisted_list_proofs_but_can_read_old_receipt(self):
        self.responses["get_friend_list"] = {"status": "ok", "retcode": 0, "data": [{"user_id": "2"}]}
        self.request("get_friend_list", {})
        op = self.event_id + ":known"
        params = {"user_id": "2", "message": "synthetic"}
        result = self.request("send_private_msg", params, operation_id=op)
        with self.store.connection() as db:
            original_journal = [tuple(row) for row in db.execute("SELECT source_ref,params,revision FROM memory_outbox ORDER BY source_ref")]
        self.service.recipients = RecipientRegistry(self.store)
        self.assertEqual(self.request("send_private_msg", params, operation_id=op), result)
        self.assertEqual(len(self.writes()), 1)
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.request("send_private_msg", params)
        with self.assertRaisesRegex(RuntimeError, "不同的动作或参数"):
            self.request("send_private_msg", {**params, "user_id": "3"}, operation_id=op)
        self.assertEqual(len(self.writes()), 1)
        with self.store.connection() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT source_ref,params,revision FROM memory_outbox ORDER BY source_ref")],
                             original_journal)

    def test_ordinary_owner_local_and_owner_group_cross_target_permissions_remain(self):
        for identity in (self.owner, LOCAL_IDENTITY, Identity("99", "1", "10", owner=True)):
            with self.subTest(identity=identity):
                result = self.service.call(identity, "send_private_msg", {"user_id": "7777777777", "message": "synthetic"},
                                           "ordinary:"+identity.conversation)
                self.assertEqual(result["status"], "ok")
                result = self.service.call(identity, "send_private_msg_async", {"user_id": "7777777777", "message": "synthetic"},
                                           "ordinary-alias:"+identity.conversation)
                self.assertEqual(result["status"], "ok")
        # A caller cannot turn a stored proactive event into ordinary authority via body fields.
        with self.assertRaisesRegex(RuntimeError, "没有可信来源"):
            self.client.request("/action", {"action": "send_private_msg", "params": {
                "user_id": "8888888888", "message": "synthetic"}, "autonomous": False,
                "operation_id": self.event_id + ":spoof"}, event_id=self.event_id)

    def test_source_lists_are_bounded_by_account_not_by_current_owner_peer(self):
        self.service.recipients.observe(self.owner, "get_friend_list", {},
                                        {"status": "ok", "retcode": 0, "data": [{"user_id": "2"}]})
        self.service.recipients.authorize(Identity("99", "3", "10", owner=True), "send_private_msg",
                                          {"user_id": "2"}, autonomous=True)
        with self.assertRaises(PermissionError):
            self.service.recipients.authorize(Identity("98", "1", owner=True), "send_private_msg",
                                              {"user_id": "2"}, autonomous=True)

    def test_recipient_ids_reject_noncanonical_values_in_autonomous_mode(self):
        for value in (True, 0, -1, "", " 2", "02", "２", "QQ:2", "owner", [2], {"id": 2}):
            with self.subTest(value=value), self.assertRaises((ValueError, PermissionError)):
                self.service.recipients.authorize(self.owner, "send_private_msg", {"user_id": value}, autonomous=True)

    def test_prompt_uses_trusted_context_instead_of_candidate_recipient_text_and_keeps_prefix(self):
        from will_runtime import build_will_conversation
        with patch("prompt_builder.build_conversation_messages", return_value=[{"role": "system", "content": "stable"}]), \
                tool_context(ToolContext(self.owner, self.event_id, self.event_id, autonomous=True)):
            messages = build_will_conversation({}, {**self.candidate, "user_id": "7777777777"})
        self.assertEqual(messages[0], {"role": "system", "content": "stable"})
        self.assertIn('"account_qq": "99"', messages[-2]["content"])
        self.assertIn('"peer_id": "1"', messages[-2]["content"])
        self.assertIn("省略 user_id/group_id", messages[-2]["content"])


if __name__ == "__main__":
    unittest.main()
