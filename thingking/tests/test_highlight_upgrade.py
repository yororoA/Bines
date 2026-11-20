"""Exercise the legacy status ledger, including receipts arriving with old QQ attribution."""
import json
import unittest

from memory.highlight_upgrade import repair_autonomous_highlights
from memory.repository import MemoryRepository
from memory.state_effects import StateEffects, _apply
from memory.temporal import TemporalStore


class HighlightUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.repo = MemoryRepository(":memory:", lambda: self.now)
        self.addCleanup(self.repo.close)
        self.repo.temporal = TemporalStore(self.repo)
        self.effects = StateEffects(self.repo)
        self.conn = self.repo.conn
        self.conn.execute("CREATE TABLE experiences (source_ref TEXT,scope TEXT,record TEXT,memory_id TEXT)")
        self.base = ["保留旧约定", "稍后明确删除"] + [f"既有记忆{i}" for i in range(30)]
        self.effects.manual("owner", {"memory_highlights": self.base, "current_location": "家"}, source_ref="manual:list")
        self.effects.manual("visitor", {"memory_highlights": ["访客约定"]})
        self.autonomous_ref = "experience:historical-action"

    def operation(self, changes, *, autonomous=False, event_ts=None, apply=True):
        self.now += 1
        state = self.repo.get_state("owner")
        key = f"operation:{self.now}"
        ref = "old-qq-message" if autonomous else key
        stamp = event_ts if event_ts is not None else self.now
        self.conn.execute("""INSERT INTO state_effects(event_key,scope,event_ts,kind,changes,source_ref,highlight_limit)
                          VALUES(?,'owner',?,?,?,?,32)""", (key, stamp, "manual_operations" if autonomous else "automatic",
                          json.dumps(changes), ref))
        if not apply:
            return
        data = _apply(state["data"], changes, key, highlight_limit=32)
        version = self.repo.put_state("owner", data, state["version"], source_ref=ref)
        if autonomous:
            receipt = {"action": "update_status", "args": changes, "autonomous": True, "source_message_refs": [],
                       "status": "succeeded", "started_at": str(self.now - .5), "finished_at": str(self.now + .5),
                       "result": f"动态状态已更新 (版本: {state['version']} -> {version})"}
            memory = self.repo.add_memory("episode", "owner", "工具经历原文")
            self.conn.execute("INSERT INTO experiences VALUES(?,'owner',?,?)",
                              (self.autonomous_ref, json.dumps(receipt), memory))

    def test_recover_verified_evictions_and_keep_explicit_removal_later_edit_and_history(self):
        self.operation({"add_memory_highlight": "资料笔记", "npc_activity": "查资料"}, autonomous=True, event_ts=10)
        self.operation({"remove_memory_highlight": self.base[1]})
        self.operation({"add_memory_highlight": "后来的约定", "npc_activity": "听歌"})
        before = self.repo.get_state("owner")
        visitor = self.repo.get_state("visitor")
        repair_autonomous_highlights(self.effects)
        now = self.repo.get_state("owner")
        self.assertEqual(now["data"]["memory_highlights"], [self.base[0], *self.base[2:], "后来的约定"])
        self.assertEqual(now["data"]["npc_state"]["current_activity"], "听歌")
        self.assertEqual(self.repo.get_state("visitor"), visitor)
        self.assertEqual(self.repo.temporal.state_history("owner", limit=10)[1]["data"], before["data"])
        row = self.conn.execute("SELECT * FROM state_effects WHERE source_ref=?", (self.autonomous_ref,)).fetchone()
        self.assertEqual((row["kind"], row["event_ts"], json.loads(row["changes"])),
                         ("autonomous_operations", 1001, {"npc_activity": "查资料"}))
        repair_autonomous_highlights(self.effects)
        self.effects._rebuild("owner", only_if_changed=True)
        self.assertEqual(now, self.repo.get_state("owner"))
        self.repo.temporal.retract_source("owner", self.autonomous_ref)
        self.assertFalse(any("资料笔记" in row["data"].get("memory_highlights", [])
                             for row in self.repo.temporal.state_history("owner")))

    def test_ambiguous_late_automatic_proposal_hidden_by_fifo_is_not_revived(self):
        self.check_late_proposal()

    def test_late_proposal_before_materialized_baseline_is_not_revived(self):
        data = self.repo.get_state("owner")["data"]
        self.conn.execute("DELETE FROM state_effects WHERE scope='owner'")
        self.conn.execute("UPDATE state_effect_baselines SET data=?,updated_at=? WHERE scope='owner'",
                          (json.dumps(data), self.now))
        self.check_late_proposal()

    def check_late_proposal(self):
        self.operation({"add_memory_highlight": "被拒的旧事实"}, event_ts=2, apply=False)
        self.operation({"add_memory_highlight": "资料笔记"}, autonomous=True, event_ts=1)
        for i in range(32):
            self.operation({"add_memory_highlight": f"后来约定{i}"})
        before = self.repo.get_state("owner")
        repair_autonomous_highlights(self.effects)
        self.assertEqual(before, self.repo.get_state("owner"))
        self.assertFalse(self.conn.execute("SELECT 1 FROM state_effects WHERE kind='autonomous_operations'").fetchone())

    def test_malformed_legacy_snapshot_or_receipt_cannot_block_startup(self):
        self.effects.manual("owner", {"memory_highlights": None})
        self.operation({"add_memory_highlight": "资料笔记"}, autonomous=True)
        before = self.repo.get_state("owner")
        repair_autonomous_highlights(self.effects)
        self.assertEqual(before, self.repo.get_state("owner"))
        self.conn.execute("DELETE FROM meta WHERE key LIKE 'autonomous_highlight_receipts_%'")
        self.conn.execute("UPDATE experiences SET record=json_set(record,'$.started_at',NULL)")
        repair_autonomous_highlights(self.effects)
        self.assertEqual(before, self.repo.get_state("owner"))
        self.conn.execute("DELETE FROM meta WHERE key LIKE 'autonomous_highlight_receipts_%'")
        self.conn.execute("UPDATE experiences SET record=json_set(record,'$.result',?)",
                          ("动态状态已更新 (版本: 9223372036854775808 -> 9223372036854775809)",))
        repair_autonomous_highlights(self.effects)
        self.assertEqual(before, self.repo.get_state("owner"))
