"""Reply provenance and envelope validation with isolated SQLite and a fake API."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatBot.capabilities import CapabilityService
from chatBot.messages import Identity, LOCAL_IDENTITY
from chatBot.store import MemoryStore
from tools.context import ToolContext, tool_context


def event(mid, *, user="2", group="", message=None, target=None):
    result = {"post_type": "message", "self_id": "99", "user_id": user,
              "message_id": mid, "message_type": "group" if group else "private",
              "sender": {"user_id": user, "nickname": "synthetic author"},
              "message": message or [{"type": "text", "data": {"text": "synthetic text"}}]}
    if group:
        result["group_id"] = group
    if target is not None:
        result["target_id"] = target
    return result


def reply(mid):
    return {"type": "reply", "data": {"id": mid}}


class FakeAPI:
    def __init__(self):
        self.result = {"status": "failed"}
        self.calls = []
        self.on_read = None

    def raw_api(self, action, params, **kwargs):
        if action != "get_msg":
            raise AssertionError("no real sends")
        self.calls.append((action, params))
        if self.on_read:
            self.on_read()
        return copy.deepcopy(self.result)


class QQQuoteValidationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bines-quote-")
        self.addCleanup(temp.cleanup)
        self.store = MemoryStore(Path(temp.name) / "gateway.db", owners={"1"})
        self.api = FakeAPI()
        self.service = CapabilityService(self.api, self.store, catalog=object())

    def incoming(self, *, user="2", group="", mid=100, quoted=80):
        value = event(mid, user=user, group=group, message=[reply(quoted),
                      {"type": "text", "data": {"text": "a new reply"}}])
        self.store.ingest(value)
        identity = self.store.identity(value)
        return identity, f"message:99:{group or 'p:'+user}:{mid}"

    def cached(self, identity, *, role="assistant", media=False, mid=80):
        chain = [{"type": "image", "data": {"file": "https://example.invalid/old.png"}}] if media else [
            {"type": "text", "data": {"text": "historical words"}}]
        self.store.add(identity, role, "historical words",
                       external_id=f"99:{identity.group or 'p:'+identity.user}:{mid}",
                       raw={"message": chain, "receipt": {"status": "ok", "data": {"message_id": mid}}})

    def recall(self, identity, mid):
        with self.store.connection() as db:
            db.execute("UPDATE messages SET recalled=1,raw=NULL WHERE scope=? AND external_id=?",
                       (identity.scope, f"99:{identity.group or 'p:'+identity.user}:{mid}"))

    def test_receipt_only_cache_identifies_assistant_instead_of_requesting_user(self):
        for user, group in [("2", ""), ("1", ""), ("1", "10")]:
            with self.subTest(user=user, group=group):
                identity, key = self.incoming(user=user, group=group)
                self.cached(identity)
                data = self.service.quoted_message(identity, key, "80")["data"]
                self.assertEqual(str(data["message_id"]), "80")
                self.assertEqual(str(data["user_id"]), "99")
                self.assertEqual(str(data["sender"]["user_id"]), "99")
                self.assertEqual(data["message_type"], "group" if group else "private")
                self.assertTrue(data["sender"]["nickname"])
        self.assertEqual(self.api.calls, [])

    def test_cached_group_quote_keeps_original_human_author(self):
        self.store.ingest(event(80, user="3", group="10"))
        identity, key = self.incoming(user="2", group="10")
        data = self.service.quoted_message(identity, key, "80")["data"]
        self.assertEqual(str(data["user_id"]), "3")
        self.assertEqual(data["sender"]["nickname"], "synthetic author")
        self.assertEqual(self.api.calls, [])

    def test_cache_without_message_content_still_fetches_the_actual_quote(self):
        identity, key = self.incoming()
        self.cached(identity)
        with self.store.connection() as db:
            db.execute("UPDATE messages SET raw='{}' WHERE external_id='99:p:2:80'")
        self.api.result = {"status": "ok", "data": event(80, user="99", target="2")}
        result = self.service.quoted_message(identity, key, "80")
        self.assertEqual(result["data"]["message"], self.api.result["data"]["message"])
        self.assertEqual(self.api.calls, [("get_msg", {"message_id": "80"})])

    def test_cached_outgoing_media_cannot_override_explicit_foreign_peer(self):
        identity, key = self.incoming(user="1")
        self.cached(identity, media=True)
        self.api.result = {"status": "ok", "data": event(80, user="99", target="3")}
        with self.assertRaises(PermissionError):
            self.service.quoted_message(identity, key, "80")

    def test_cached_outgoing_media_allows_omitted_peer_and_preserves_source_identity(self):
        identity, key = self.incoming()
        self.cached(identity, media=True)
        self.api.result = {"status": "ok", "data": event(80, user="99")}
        self.assertEqual(self.service.quoted_message(identity, key, "80")["status"], "ok")

    def test_uncached_outgoing_media_needs_explicit_peer(self):
        identity, key = self.incoming()
        self.api.result = {"status": "ok", "data": event(80, user="99")}
        with self.assertRaises(PermissionError):
            self.service.quoted_message(identity, key, "80")
        self.api.result["data"]["target_id"] = "2"
        self.assertEqual(self.service.quoted_message(identity, key, "80")["status"], "ok")

    def test_refresh_wrong_account_message_or_group_is_rejected(self):
        identity, key = self.incoming(group="10")
        for changed in [{"self_id": "98"}, {"message_id": 81}, {"group_id": "20"}]:
            with self.subTest(changed=changed):
                self.api.result = {"status": "ok", "data": {**event(80, group="10"), **changed}}
                with self.assertRaises(PermissionError):
                    self.service.quoted_message(identity, key, "80")

    def test_recall_during_refresh_suppresses_fresh_and_cached_contents(self):
        for status in ("ok", "failed"):
            with self.subTest(status=status):
                identity, key = self.incoming(mid=100 if status == "ok" else 101,
                                              quoted=80 if status == "ok" else 81)
                mid = 80 if status == "ok" else 81
                self.cached(identity, media=True, mid=mid)
                self.api.result = {"status": status, "data": event(mid)}
                self.api.on_read = lambda: self.recall(identity, mid)
                result = self.service.quoted_message(identity, key, str(mid))
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("data", result)

    def test_withdrawn_current_event_cannot_still_authorize_a_quote(self):
        identity, key = self.incoming()
        self.cached(identity)
        self.recall(identity, 100)
        result = self.service.quoted_message(identity, key, "80")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("data", result)

    def test_batch_event_and_unreferenced_id_remain_distinct(self):
        identity, first = self.incoming(quoted=80)
        _, second = self.incoming(mid=101, quoted=81)
        self.cached(identity, mid=80)
        self.cached(identity, mid=81)
        for key, mid in [(first, 81), (second, 80)]:
            with self.assertRaises(PermissionError):
                self.service.quoted_message(identity, key, str(mid))
        self.assertEqual(self.service.quoted_message(identity, first, "80")["status"], "ok")
        self.assertEqual(self.service.quoted_message(identity, second, "81")["status"], "ok")

    def test_all_identities_reject_multiple_or_invalid_reply_segments(self):
        ordinary = Identity("99", "2")
        owner = Identity("99", "1", owner=True)
        chains = [[reply("80"), reply("81")], [reply(True)], [reply(0)], [reply("%")],
                  [reply("８０")], [reply(1.5)]]
        for identity in (ordinary, owner, LOCAL_IDENTITY):
            for chain in chains:
                with self.subTest(identity=identity, chain=chain):
                    with self.assertRaises((ValueError, PermissionError)):
                        self.service.prepare(identity, "send_private_msg", {"user_id": identity.user, "message": chain})
        for chain in chains:
            with self.assertRaises((ValueError, PermissionError)):
                self.service.prepare(owner, "send_private_forward_msg", {"user_id": "1", "messages": [
                    {"type": "node", "data": {"user_id": "99", "content": chain}}]})

    def test_legal_historical_assistant_reply_and_literal_cq_text_are_preserved(self):
        identity, _ = self.incoming()
        self.cached(identity)
        params = {"user_id": "2", "message": [reply("80"), {"type": "text", "data": {"text": "yes"}}]}
        self.assertEqual(self.service.prepare(identity, "send_private_msg", params), params)
        params = {"user_id": "2", "message": "[CQ:reply,id=0] is just text", "auto_escape": True}
        prepared = self.service.prepare(identity, "send_private_msg", params)
        self.assertEqual(prepared["message"][0]["type"], "text")

    def test_napcat_send_rejects_conflicting_or_invalid_reply_before_gateway(self):
        from tools.napcat_tool import napcat_send
        with patch("tools.napcat_tool.GatewayClient") as gateway, tool_context(ToolContext(Identity("99", "1", owner=True))):
            for value in (True, 0, "", "-1", "1.5", "８０"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    napcat_send("hello", reply_to=value)
            with self.assertRaises(ValueError):
                napcat_send([reply("80")], reply_to="81")
            gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
