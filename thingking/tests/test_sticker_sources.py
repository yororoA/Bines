"""Authorized source resolution using temporary databases; no QQ/model calls."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]

from chatBot.messages import Identity, LOCAL_IDENTITY
from chatBot.sticker_sources import resolve_favorite_source, resolve_sticker_source, validate_sticker_source


def picture(name="old"):
    return {"type": "image", "data": {"file": "opaque-file", "url": f"https://example.invalid/{name}.gif",
                                      "sub_type": 1, "summary": "synthetic sticker"}}


def event(mid=100, *, user="1", group=None, chain=None, target=None):
    result = {"self_id": 99, "post_type": "message", "message_type": "group" if group else "private",
              "user_id": user, "message_id": mid, "time": time.time(),
              "sender": {"user_id": user, "nickname": "synthetic", "role": "member"},
              "message": chain if chain is not None else [picture()]}
    if group:
        result["group_id"] = group
    if target:
        result["target_id"] = target
    return result


class ReadOnlyNapCat:
    def __init__(self):
        self.messages = {}
        self.favorites = []
        self.calls = []
        self.on_read = None

    def raw_api(self, action, params, timeout=30):
        self.calls.append((action, dict(params)))
        if self.on_read:
            self.on_read()
        if action == "get_msg":
            value = self.messages.get(str(params["message_id"]))
            return {"status": "ok", "data": copy.deepcopy(value)} if value is not None else {"status": "failed"}
        if action == "fetch_custom_face_detail":
            return {"status": "ok", "data": copy.deepcopy(self.favorites[:params["count"]])}
        raise AssertionError("source resolver must not invoke QQ writes or any other API")


class StickerSourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bines-sticker-sources-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        environ = patch.dict(os.environ, {
            "ENV_FILE": "/dev/null", "BINES_ENV_FILE": "/dev/null",
            "BINES_MEMORY_DIR": str(base / "memory"), "BINES_GATEWAY_DIR": str(base / "gateway"),
            "BINES_HEALTH_DIR": str(base / "health"), "BINES_MODEL_USAGE_PATH": str(base / "usage.db"),
            "BINES_WEBSITE_BRIDGE_SPOOL": str(base / "spool"), "HF_HUB_OFFLINE": "1",
        })
        environ.start()
        self.addCleanup(environ.stop)
        from chatBot.store import MemoryStore
        from chatBot.capabilities import CapabilityService
        self.store = MemoryStore(base / "gateway.db", owners={"1"})
        self.api = ReadOnlyNapCat()
        self.service = CapabilityService(self.api, self.store, catalog=object())

    def insert(self, value):
        self.store.ingest(value)
        who = self.store.identity(value)
        key = f"message:{who.account}:{who.group or 'p:'+who.user}:{value['message_id']}"
        return who, key

    def withdraw(self, mid, identity):
        external = f"{identity.account}:{identity.group or 'p:'+identity.user}:{mid}"
        with self.store.connection() as db:
            db.execute("UPDATE messages SET recalled=1,raw=NULL WHERE external_id=?", (external,))

    def test_current_image_refreshes_url_and_preserves_original_source_id(self):
        value = event()
        who, key = self.insert(value)
        self.api.messages["100"] = event(chain=[picture("fresh")])
        result = resolve_sticker_source(self.service, who, key)
        self.assertEqual(result["segment"]["data"]["url"], "https://example.invalid/fresh.gif")
        self.assertEqual(result["source_message_id"], "100")
        self.assertTrue(result["source_refreshed"])
        self.assertEqual(self.api.calls, [("get_msg", {"message_id": "100"})])
        with self.store.connection() as db:
            saved = db.execute("SELECT raw FROM messages").fetchone()[0]
        self.assertIn("old.gif", saved)  # Resolution never rewrites persisted messages.
        self.assertNotIn("fresh.gif", saved)

    def test_index_counts_only_image_face_and_marketplace_segments(self):
        face = {"type": "face", "data": {"id": "0"}}
        mface = {"type": "mface", "data": {"emoji_package_id": "10", "emoji_id": "20", "key": "synthetic-key"}}
        who, key = self.insert(event(chain=[{"type": "text", "data": {"text": "save"}}, face,
                                           picture(), {"type": "at", "data": {"qq": "99"}}, mface]))
        for index, expected in enumerate([face, picture(), mface]):
            with self.subTest(index=index):
                result = resolve_sticker_source(self.service, who, key, image_index=index)
                self.assertEqual(result["segment"], expected)
        with self.assertRaisesRegex(ValueError, "只有 3 个"):
            resolve_sticker_source(self.service, who, key, image_index=3)

    def test_refresh_failure_retains_only_previously_authorized_original(self):
        who, key = self.insert(event())
        result = resolve_sticker_source(self.service, who, key)
        self.assertFalse(result["source_refreshed"])
        self.assertEqual(result["segment"], picture())

    def test_reference_without_local_source_row_uses_quoted_message_authority(self):
        who, key = self.insert(event(chain=[{"type": "reply", "data": {"id": "80"}},
                                           {"type": "text", "data": {"text": "save this"}}]))
        self.api.messages["80"] = event(80, chain=[picture("quoted")])
        with patch.object(self.service, "quoted_message", wraps=self.service.quoted_message) as quoted:
            result = resolve_sticker_source(self.service, who, key)
        quoted.assert_called_once_with(who, key, "80")
        self.assertEqual(result["source_message_id"], "80")
        self.assertIsNone(result["source_refreshed"])
        self.assertEqual(result["segment"]["data"]["url"], "https://example.invalid/quoted.gif")

    def test_current_candidates_take_priority_over_reply_and_explicit_can_choose_reply(self):
        who, key = self.insert(event(chain=[{"type": "reply", "data": {"id": "80"}}, picture()]))
        self.api.messages["80"] = event(80, chain=[picture("quoted")])
        result = resolve_sticker_source(self.service, who, key)
        self.assertEqual(result["source_message_id"], "100")
        explicit = resolve_sticker_source(self.service, who, key, message_id="80")
        self.assertEqual(explicit["source_message_id"], "80")

    def test_multiple_replies_share_candidate_index_and_keep_selected_message_id(self):
        who, key = self.insert(event(chain=[{"type": "reply", "data": {"id": str(mid)}} for mid in (80, 81)]))
        for mid in (80, 81):
            self.api.messages[str(mid)] = event(mid, chain=[picture(str(mid))])
        result = resolve_sticker_source(self.service, who, key, image_index=1)
        self.assertEqual(result["source_message_id"], "81")

    def test_explicit_source_from_same_group_can_be_another_speaker(self):
        who, key = self.insert(event(group="10", chain=[{"type": "text", "data": {"text": "save"}}]))
        self.insert(event(80, user="2", group="10"))
        self.api.messages["80"] = event(80, user="2", group="10", chain=[picture("fresh")])
        result = resolve_sticker_source(self.service, who, key, message_id=80)
        self.assertEqual(result["source_message_id"], "80")

    def test_owner_cannot_read_other_private_or_group_sources_by_message_id(self):
        owner, key = self.insert(event())
        self.insert(event(80, user="2"))
        self.insert(event(81, group="20"))
        for mid in (80, 81):
            with self.subTest(mid=mid), self.assertRaises(PermissionError):
                resolve_sticker_source(self.service, owner, key, message_id=mid)
        self.assertEqual(self.api.calls, [])
        group_owner, group_key = self.insert(event(110, group="10"))
        with self.assertRaises(PermissionError):
            resolve_sticker_source(self.service, group_owner, group_key, message_id=81)

    def test_wrong_event_identity_or_local_message_lookup_is_denied(self):
        owner, key = self.insert(event())
        stranger, _ = self.insert(event(101, user="2"))
        for who in (stranger, LOCAL_IDENTITY):
            with self.subTest(who=who.user), self.assertRaises(PermissionError):
                resolve_sticker_source(self.service, who, key, message_id=100)
        self.assertEqual(self.api.calls, [])

    def test_withdrawn_current_or_referenced_source_is_denied_before_fetch(self):
        who, key = self.insert(event())
        self.withdraw(100, who)
        with self.assertRaisesRegex(PermissionError, "已撤回"):
            resolve_sticker_source(self.service, who, key)
        who, key = self.insert(event(101, chain=[{"type": "reply", "data": {"id": "100"}}]))
        with self.assertRaisesRegex(PermissionError, "已撤回"):
            resolve_sticker_source(self.service, who, key)
        self.assertEqual(self.api.calls, [])

    def test_withdrawal_during_refresh_is_rechecked(self):
        who, key = self.insert(event())
        self.api.messages["100"] = event(chain=[picture("fresh")])
        self.api.on_read = lambda: self.withdraw(100, who)
        with self.assertRaisesRegex(PermissionError, "已撤回"):
            resolve_sticker_source(self.service, who, key)

    def test_refreshed_message_id_account_and_private_peer_must_match(self):
        who, key = self.insert(event())
        cases = [event(101), {**event(), "self_id": 98}, event(user="2"), event(group="20")]
        for fresh in cases:
            with self.subTest(shape=(fresh["message_id"], fresh["self_id"], fresh["message_type"])):
                self.api.messages["100"] = fresh
                with self.assertRaises(PermissionError):
                    resolve_sticker_source(self.service, who, key)

    def test_cached_sent_message_does_not_override_an_explicit_wrong_peer(self):
        who, key = self.insert(event(chain=[{"type": "text", "data": {"text": "save"}}]))
        self.insert(event(80, user="99", target="1"))
        self.api.messages["80"] = event(80, user="99", target="2")
        with self.assertRaisesRegex(PermissionError, "当前 QQ 会话"):
            resolve_sticker_source(self.service, who, key, message_id=80)
        self.api.messages["80"] = event(80, user="99")
        self.assertEqual(resolve_sticker_source(self.service, who, key, message_id=80)["source_message_id"], "80")

    def test_foreign_quoted_response_cannot_bypass_owner_authority(self):
        who, key = self.insert(event(group="10", chain=[{"type": "reply", "data": {"id": "80"}}]))
        self.api.messages["80"] = event(80, group="20")
        with self.assertRaises(PermissionError):
            resolve_sticker_source(self.service, who, key)

    def test_missing_candidate_and_invalid_selection_have_specific_errors(self):
        who, key = self.insert(event(chain=[{"type": "text", "data": {"text": "no image"}}]))
        with self.assertRaisesRegex(ValueError, "当前消息没有图片"):
            resolve_sticker_source(self.service, who, key)
        for index in (-1, True, 0.5, "0"):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "image_index"):
                resolve_sticker_source(self.service, who, key, image_index=index)
        for mid in ("", "%", True, -1):
            with self.subTest(mid=mid), self.assertRaisesRegex(ValueError, "message_id"):
                resolve_sticker_source(self.service, who, key, message_id=mid)
        broken, broken_key = self.insert(event(101, chain=[{"type": "mface", "data": {"emoji_id": "1"}}]))
        with self.assertRaisesRegex(ValueError, "商城表情缺少"):
            resolve_sticker_source(self.service, broken, broken_key)

    def proactive_event(self):
        # In production the gateway verifies this persisted candidate/lease
        # before calling the resolver. The resolver still enforces source scope.
        value = event(chain=[])
        value.pop("message_id")
        value.update(post_type="bines_proactive", proactive={"contact_allowed": False})
        who = self.store.identity(value)
        with self.store.connection() as db:
            db.execute("INSERT INTO events(id,conversation,raw,received) VALUES(?,?,?,?)",
                       ("synthetic-proactive", who.conversation, json.dumps(value), time.time()))
        return who, "synthetic-proactive"

    def test_proactive_event_can_resolve_only_an_explicit_scoped_history_source(self):
        self.insert(event(80))
        who, key = self.proactive_event()
        self.api.messages["80"] = event(80, chain=[picture("historical-fresh")])
        result = resolve_sticker_source(self.service, who, key, message_id=80)
        self.assertEqual(result["source_message_id"], "80")
        self.assertTrue(result["source_refreshed"])
        self.assertEqual(result["segment"]["data"]["url"], "https://example.invalid/historical-fresh.gif")

    def test_proactive_event_never_guesses_latest_or_grants_uncached_reply_access(self):
        self.insert(event(80))
        who, key = self.proactive_event()
        with self.assertRaisesRegex(ValueError, "必须明确指定"):
            resolve_sticker_source(self.service, who, key)
        with self.store.connection() as db:
            row = db.execute("SELECT raw FROM events WHERE id=?", (key,)).fetchone()
            raw = json.loads(row["raw"])
            raw["message"] = [{"type": "reply", "data": {"id": "81"}}]
            db.execute("UPDATE events SET raw=? WHERE id=?", (json.dumps(raw), key))
        with self.assertRaises(PermissionError):
            resolve_sticker_source(self.service, who, key, message_id=81)
        self.assertEqual(self.api.calls, [])

    def test_proactive_explicit_history_cannot_cross_scope_or_restore_withdrawal(self):
        self.insert(event(80, user="2"))
        owner, _ = self.insert(event(81))
        self.withdraw(81, owner)
        who, key = self.proactive_event()
        for mid in (80, 81):
            with self.subTest(mid=mid), self.assertRaises(PermissionError):
                resolve_sticker_source(self.service, who, key, message_id=mid)
        self.assertEqual(self.api.calls, [])

    def test_post_download_validation_checks_withdrawal_without_network(self):
        who, key = self.insert(event())
        self.assertTrue(validate_sticker_source(self.service, who, key, "100"))
        self.withdraw(100, who)
        with self.assertRaisesRegex(PermissionError, "已撤回"):
            validate_sticker_source(self.service, who, key, "100")
        self.assertEqual(self.api.calls, [])

    def test_post_download_validation_preserves_real_uncached_reply_and_tombstone(self):
        who, key = self.insert(event(chain=[{"type": "reply", "data": {"id": "80"}}]))
        self.assertTrue(validate_sticker_source(self.service, who, key, "80"))
        self.insert(event(80))
        self.withdraw(80, who)
        with self.assertRaisesRegex(PermissionError, "已撤回"):
            validate_sticker_source(self.service, who, key, "80")
        self.assertEqual(self.api.calls, [])

    def test_post_download_validation_never_grants_cross_scope_or_proactive_reply(self):
        self.insert(event(80, user="2"))
        who, key = self.proactive_event()
        for mid in (80, 81):
            with self.subTest(mid=mid), self.assertRaises(PermissionError):
                validate_sticker_source(self.service, who, key, mid)
        self.assertEqual(self.api.calls, [])

    def test_favorite_uses_stable_md5_when_order_changes(self):
        md5 = "a" * 32
        target = {"md5": md5.upper(), "url": "https://example.invalid/favorite.gif", "desc": "synthetic",
                  "resId": "synthetic-resource", "emojiId": "4", "packageId": 0}
        other = {"md5": "b" * 32, "url": "https://example.invalid/other.png"}
        for rows in ([target, other], [other, target]):
            self.api.favorites = rows
            result = resolve_favorite_source(self.service, LOCAL_IDENTITY, md5)
            self.assertEqual(result["segment"]["data"]["file"], target["url"])
            self.assertEqual(result["favorite"]["md5"], md5)
            self.assertEqual(result["favorite"]["res_id"], "synthetic-resource")
            self.assertIsNone(result["source_message_id"])

    def test_favorites_allow_local_and_owner_private_but_not_owner_group(self):
        self.api.favorites = [{"md5": "a" * 32, "url": "https://example.invalid/a.png"}]
        for who in (LOCAL_IDENTITY, Identity("99", "1", owner=True)):
            self.assertEqual(resolve_favorite_source(self.service, who, "a" * 32)["segment"]["type"], "image")
        before = len(self.api.calls)
        for who in (Identity("99", "1", group="10", owner=True), Identity("99", "2")):
            with self.subTest(who=who), self.assertRaises(PermissionError):
                resolve_favorite_source(self.service, who, "a" * 32)
        self.assertEqual(len(self.api.calls), before)

    def test_favorite_missing_md5_or_url_fails_without_substituting_another(self):
        with self.assertRaisesRegex(ValueError, "没有该 MD5"):
            resolve_favorite_source(self.service, LOCAL_IDENTITY, "a" * 32)
        self.api.favorites = [{"md5": "a" * 32}]
        with self.assertRaisesRegex(ValueError, "没有可读取"):
            resolve_favorite_source(self.service, LOCAL_IDENTITY, "a" * 32)
        with self.assertRaisesRegex(ValueError, "32 位 MD5"):
            resolve_favorite_source(self.service, LOCAL_IDENTITY, "not-md5")


if __name__ == "__main__":
    unittest.main()
