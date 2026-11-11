"""Withdrawn message effects must disappear from current state and prompts."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class MemoryStateRetractionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bines-state-retraction-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(self.root),
            "BINES_GATEWAY_DIR": str(self.root / "gateway"),
            "BINES_HEALTH_DIR": str(self.root / "health"),
            "BINES_MODEL_USAGE_PATH": str(self.root / "usage.db"),
        })
        env.start()
        self.addCleanup(env.stop)
        from memory.repository import MemoryRepository
        from memory.settings import MemorySettings
        from memory.service import MemoryService
        from memory.lexical_index import LexicalIndex
        self.now = 1_792_800_000.0
        self.repo = MemoryRepository(self.root / "memory.db", lambda: self.now)
        self.addCleanup(self.repo.close)
        settings = MemorySettings(data_dir=self.root, owner_qq_ids=frozenset({"10001"}),
                                  state_updates=True, associations_enabled=False,
                                  summarize_trigger=1000)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), settings, lambda: self.now)
        self.service.sync_identities()

    def envelope(self, user=None, group=None):
        from memory.channels import local_envelope, qq_envelope
        if user is None:
            return local_envelope(timestamp=self.now).to_dict()
        return qq_envelope({"self_id": "bot", "user_id": user, "group_id": group or ""},
                           timestamp=self.now).to_dict()

    def record(self, ref, text, *, user=None, group=None):
        envelope = self.envelope(user, group)
        self.service.record_message(envelope, "user", text, source_ref=ref)
        return envelope

    def complete(self, changes):
        claim = self.service.claim_job(["update_state"])
        self.assertIsNotNone(claim)
        result = self.service.complete_state_update(claim["job"]["id"], changes,
            expected_version=claim["materials"]["version"], expected_attempt=claim["job"]["attempts"])
        self.assertEqual(result["status"], "done")
        return claim

    def withdraw(self, ref, envelope):
        self.service.record_message(envelope, "user", "[消息已撤回]", source_ref=ref, recalled=True)

    def manual_turn(self, turn, text, **changes):
        from types import SimpleNamespace
        from memory.client import MemoryClient
        from tools.context import CURRENT_CONTEXT, ToolContext
        from tools.dependencies import deps
        from tools.memory_tool import update_status
        envelope = self.record(turn + ":user", text)
        client = MemoryClient.__new__(MemoryClient)
        client.call = lambda method, **params: getattr(self.service, method)(**params)
        token = CURRENT_CONTEXT.set(ToolContext(journal=SimpleNamespace(prefix=turn)))
        try:
            with patch.object(deps, "memory_client", client):
                self.assertIn("动态状态已更新", update_status(**changes))
        finally:
            CURRENT_CONTEXT.reset(token)
        return envelope

    def test_owner_rebuild_preserves_baseline_and_later_independent_effects(self):
        self.service.put_state("owner", {"current_location": "北京", "relationship_score": 10})
        old = self.record("owner-old", "到上海，拿到书", user="10001")
        self.complete({"current_location": "上海", "add_item": "书", "relationship_delta": 2})
        self.now += 1
        self.record("desktop-new", "准备读书，拿到笔")
        self.complete({"active_quest": "读书", "add_item": "笔", "relationship_delta": 1})
        self.withdraw("owner-old", old)
        state = self.service.get_state("owner")["data"]
        self.assertEqual((state["current_location"], state["relationship_score"], state["active_quest"]),
                         ("北京", 11, "读书"))
        self.assertEqual(state["inventory"], ["笔"])
        self.assertEqual(self.service.build_context(self.envelope(), "我在哪")['state'], state)

    def test_newer_location_and_unsourced_manual_update_survive(self):
        old = self.record("old", "我在上海")
        self.complete({"current_location": "上海"})
        self.now += 1
        self.record("new", "现在到杭州了")
        self.complete({"current_location": "杭州"})
        current = self.service.get_state("owner")["data"]
        self.service.put_state("owner", {**current, "active_quest": "人工保留的约定"})
        self.withdraw("old", old)
        state = self.service.get_state("owner")["data"]
        self.assertEqual((state["current_location"], state["active_quest"]), ("杭州", "人工保留的约定"))

    def test_group_retraction_rebuilds_only_the_speaker_state(self):
        a_scope, b_scope = "qq:bot:group:group1:user:20001", "qq:bot:group:group1:user:20002"
        self.service.put_state("owner", {"current_location": "主人地点"})
        self.service.put_state(a_scope, {"current_location": "北京"})
        old = self.record("group-a", "我到上海了", user="20001", group="group1")
        self.complete({"current_location": "上海"})
        self.now += 1
        self.record("group-b", "我到广州了", user="20002", group="group1")
        self.complete({"current_location": "广州"})
        self.withdraw("group-a", old)
        self.assertEqual(self.service.get_state(a_scope)["data"]["current_location"], "北京")
        self.assertEqual(self.service.get_state(b_scope)["data"]["current_location"], "广州")
        self.assertEqual(self.service.get_state("owner")["data"]["current_location"], "主人地点")
        self.assertEqual(self.service.build_context(old, "我在哪")["state"]["current_location"], "北京")

    def test_sourced_manual_patch_is_retracted_and_cannot_be_reapplied(self):
        scope = "qq:bot:private:20001"
        self.service.put_state(scope, {"current_location": "北京", "active_quest": "保留约定"})
        old = self.record("manual-source", "现在到上海了", user="20001")
        self.service.put_state(scope, {"current_location": "上海", "active_quest": "保留约定"},
                               source_ref="manual-source")
        self.withdraw("manual-source", old)
        state = self.service.get_state(scope)["data"]
        self.assertEqual(state, {"current_location": "北京", "active_quest": "保留约定"})
        with self.assertRaisesRegex(ValueError, "retracted"):
            self.service.put_state(scope, {"current_location": "上海"}, source_ref="manual-source")
        self.assertEqual(self.service.get_state(scope)["data"], state)

    def test_repeated_withdrawal_is_idempotent_and_cannot_restore_another_retracted_source(self):
        first = self.record("first", "我在上海")
        self.complete({"current_location": "上海"})
        self.now += 1
        second = self.record("second", "我在杭州")
        self.complete({"current_location": "杭州"})
        self.withdraw("first", first)
        self.withdraw("second", second)
        state = self.service.get_state("owner")
        self.assertNotIn("current_location", state["data"])
        self.withdraw("second", second)
        self.withdraw("first", first)
        self.assertEqual(self.service.get_state("owner"), state)

    def test_legacy_sourced_snapshots_are_preserved_before_history_redaction(self):
        self.repo.put_state("owner", {"current_location": "北京"})
        old = self.record("legacy-source", "我在上海")
        self.repo.put_state("owner", {"current_location": "上海"}, source_ref="legacy-source")
        self.repo.put_state("owner", {"current_location": "上海", "active_quest": "后来约定"})
        self.withdraw("legacy-source", old)
        self.assertEqual(self.service.get_state("owner")["data"],
                         {"current_location": "北京", "active_quest": "后来约定"})

    def test_withdrawal_cancels_running_state_job_before_its_result_arrives(self):
        old = self.record("inflight", "我在上海")
        claim = self.service.claim_job(["update_state"])
        self.withdraw("inflight", old)
        result = self.service.complete_state_update(claim["job"]["id"], {"current_location": "上海"},
            expected_version=claim["materials"]["version"], expected_attempt=claim["job"]["attempts"])
        self.assertEqual(result["status"], "ignored")
        self.assertNotIn("current_location", self.service.get_state("owner")["data"])

    def test_manual_same_value_confirmation_survives_retraction_of_earlier_source(self):
        old = self.manual_turn("manual-a", "我在上海", current_location="上海")
        self.now += 1
        self.manual_turn("manual-b", "我确认仍在上海", current_location="上海")
        self.withdraw("manual-a:user", old)
        self.assertEqual(self.service.get_state("owner")["data"].get("current_location"), "上海")

    def test_manual_operations_do_not_turn_inherited_fields_into_independent_evidence(self):
        old = self.manual_turn("manual-a", "我在上海，拿到书", current_location="上海", add_item="书")
        self.now += 1
        self.manual_turn("manual-b", "准备读书，拿到笔", active_quest="读书", add_item="笔")
        self.withdraw("manual-a:user", old)
        state = self.service.get_state("owner")["data"]
        self.assertNotIn("current_location", state)
        self.assertEqual(state["active_quest"], "读书")
        self.assertEqual(state["inventory"], ["笔"])

    def test_manual_confirmation_of_one_item_does_not_reconfirm_the_whole_inventory(self):
        old = self.manual_turn("manual-a", "拿到书", add_item="书")
        self.now += 1
        key = self.manual_turn("manual-key", "拿到钥匙", add_item="钥匙")
        self.now += 1
        self.manual_turn("manual-b", "确认书还在", add_item="书")
        self.withdraw("manual-a:user", old)
        self.withdraw("manual-key:user", key)
        self.assertEqual(self.service.get_state("owner")["data"]["inventory"], ["书"])

    def test_unchanged_rebuilt_state_still_has_a_clean_current_history_snapshot(self):
        old = self.record("history-a", "我在上海")
        self.complete({"current_location": "上海"})
        self.now += 1
        self.record("history-b", "现在到杭州")
        self.complete({"current_location": "杭州"})
        self.withdraw("history-a", old)
        history = self.service.state_history("owner")
        self.assertTrue(history)
        self.assertEqual(history[0]["data"]["current_location"], "杭州")
        self.assertNotIn("上海", str(history))
        current = self.service.get_state("owner")
        self.assertEqual(history[0]["version"], current["version"])
        self.withdraw("history-a", old)
        self.assertEqual(self.service.get_state("owner"), current)

    def test_late_source_message_is_linked_before_retraction(self):
        self.service.put_state("owner", {"current_location": "上海"}, source_ref="late:user",
                               changes={"current_location": "上海"})
        self.now += 1
        old = self.record("late:user", "我在上海")
        self.service.put_state("owner", {}, changes={"active_quest": "后来约定"})
        self.withdraw("late:user", old)
        state = self.service.get_state("owner")["data"]
        self.assertNotIn("current_location", state)
        self.assertEqual(state["active_quest"], "后来约定")

    def test_late_group_source_links_only_to_its_speaker_scope(self):
        scope = "qq:bot:group:group1:user:20001"
        self.service.put_state(scope, {}, source_ref="late-group", changes={"current_location": "上海"})
        old = self.record("late-group", "我在上海", user="20001", group="group1")
        self.withdraw("late-group", old)
        self.assertNotIn("current_location", self.service.get_state(scope)["data"])

    def test_late_source_first_arriving_as_recalled_still_removes_its_effect(self):
        self.service.put_state("owner", {}, source_ref="already-recalled", changes={"current_location": "上海"})
        version = self.service.get_state("owner")["version"]
        self.withdraw("already-recalled", self.envelope())
        self.assertNotIn("current_location", self.service.get_state("owner")["data"])
        self.assertEqual(self.service.get_state("owner")["version"], version + 1)

    def test_late_qq_source_retimes_clocks_before_newer_desktop_state_update(self):
        self.service.put_state("owner", {}, source_ref="late-a", changes={"current_location": "上海"})
        before_link = self.service.get_state("owner")
        late = {**self.envelope("10001"), "timestamp": self.now - 100}
        self.service.record_message(late, "user", "我在上海", source_ref="late-a", state_update=False)
        self.assertEqual(self.service.get_state("owner"), before_link)
        newer = {**self.envelope(), "timestamp": self.now - 50}
        self.service.record_message(newer, "user", "现在杭州", source_ref="desktop-b")
        self.complete({"current_location": "杭州"})
        state = self.service.get_state("owner")
        self.assertEqual(state["data"]["current_location"], "杭州")
        self.service.record_message(late, "user", "我在上海", source_ref="late-a", state_update=False)
        self.assertEqual(self.service.get_state("owner"), state)

    def test_late_source_rebuild_recovers_newer_effect_previously_filtered_by_delivery_time(self):
        self.service.put_state("owner", {}, source_ref="late-a", changes={"current_location": "上海"})
        newer = {**self.envelope(), "timestamp": self.now - 50}
        self.service.record_message(newer, "user", "现在杭州", source_ref="desktop-b")
        self.complete({"current_location": "杭州"})
        late = {**self.envelope("10001"), "timestamp": self.now - 100}
        self.service.record_message(late, "user", "我在上海", source_ref="late-a", state_update=False)
        self.assertEqual(self.service.get_state("owner")["data"]["current_location"], "杭州")

    def test_late_manual_source_does_not_enqueue_a_duplicate_automatic_relationship_effect(self):
        self.service.put_state("owner", {}, source_ref="late-manual",
                               changes={"relationship_delta": 1})
        late = {**self.envelope("10001"), "timestamp": self.now - 10}
        for _ in range(2):
            self.service.record_message(late, "user", "谢谢你", source_ref="late-manual")
            self.assertIsNone(self.service.claim_job(["update_state"]))
            self.assertEqual(self.service.get_state("owner")["data"]["relationship_score"], 1)

    def test_foreign_message_alias_cannot_retract_or_rebind_owner_state(self):
        self.service.put_state("owner", {}, source_ref="shared-alias", changes={"current_location": "北京"})
        owner = self.service.get_state("owner")
        foreign = self.record("shared-alias", "我在上海", user="20001")
        self.withdraw("shared-alias", foreign)
        self.assertEqual(self.service.get_state("owner"), owner)
        self.assertEqual(self.service.state_history("owner")[0]["data"]["current_location"], "北京")

    def test_existing_foreign_message_is_not_accepted_as_owner_state_source(self):
        self.record("foreign-source", "我在上海", user="20001")
        with self.assertRaisesRegex(ValueError, "scope"):
            self.service.put_state("owner", {}, source_ref="foreign-source", changes={"current_location": "上海"})
        self.assertEqual(self.service.get_state("owner")["data"], {})

    def test_state_operation_receipt_returns_the_committed_state(self):
        result = self.service.put_state("owner", {"relationship_level": "客户端抽样"},
                                        changes={"relationship_delta": 1})
        self.assertEqual(result.get("data"), self.service.get_state("owner")["data"])

    def test_expected_state_uses_committed_receipt_with_legacy_fallback(self):
        from types import SimpleNamespace
        from tools.context import CURRENT_CONTEXT, ToolContext
        from tools.dependencies import deps
        from tools.memory_tool import update_status
        local = {"relationship_level": "客户端抽样"}
        for receipt, expected, warning in (
            ({"version": 1, "data": {"relationship_level": "服务端抽样"}}, "服务端抽样", False),
            ({"version": 1, "data": {"relationship_level": "服务端抽样"}}, "客户端抽样", True),
            ({"version": 1}, "客户端抽样", False),
        ):
            with self.subTest(receipt=receipt, expected=expected):
                client = SimpleNamespace(get_state=lambda scope: {"data": {}, "version": 0},
                                         put_state=lambda *args, **kwargs: receipt)
                token = CURRENT_CONTEXT.set(ToolContext())
                try:
                    with patch.object(deps, "memory_client", client), \
                            patch("tools.memory_tool._apply_changes", return_value=local):
                        result = update_status(relationship_delta=1,
                                               expected_state={"relationship_level": expected})
                finally:
                    CURRENT_CONTEXT.reset(token)
                self.assertEqual("警告：relationship_level" in result, warning)


if __name__ == "__main__":
    unittest.main()
