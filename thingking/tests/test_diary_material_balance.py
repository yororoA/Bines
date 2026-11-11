"""Isolated diary selection, evidence validity and revision preservation."""
from helpers import approved_diary
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import Clock
from memory.diary_materials import POLICY, select_materials
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.worker import MemoryWorker, build_diary_prompt


class DiaryMaterialBalanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bines-diary-balance-")
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(1792800000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.tmp.name), state_updates=False, associations_enabled=False,
                           association_backfill_batch=0), self.clock)
        self.ts = self.clock() - 86400
        self.day = self.service._day(self.ts)
        self.repo.ensure_conversation("local", "local", "owner")

    def episode(self, content="约好周末看电影", *, scope="owner", messages=True):
        ids = []
        if messages:
            ids = [self.repo.add_message("local", scope, "user", "我今天其实有点委屈，但还是想和你看电影", self.ts,
                                        self.day, actor_id="owner"),
                   self.repo.add_message("local", scope, "assistant", "周末的电影我记着呢", self.ts + 1,
                                        self.day, actor_id="assistant")]
        mid = self.repo.add_memory("episode", scope, content, day_key=self.day, end_ts=self.ts,
                                   meta={"source_message_ids": ids})
        for source in ids:
            self.repo.conn.execute("UPDATE messages SET episode_id=? WHERE id=?", (mid, source))
        return self.repo.get_memory(mid)

    def experience(self, i, *, activity="watch-a", status="succeeded"):
        ref = f"test:observation:{i}"
        meta = {"experience_ref": ref, "action": "website_watch", "status": status}
        mid = self.repo.add_memory("episode", "owner", f"观察{i}：看到湖边的飞鸟，继续观看。" * 30,
                                   day_key=self.day, end_ts=self.ts + i, origin="experience", meta=meta)
        record = {"candidate": {"activity": {"id": activity}}, "action": "website_watch", "status": status}
        self.repo.conn.execute("INSERT INTO experiences VALUES(?,?,?,?,?,?)",
                               (ref, mid, "owner", json.dumps(record), 1, self.clock()))
        return self.repo.get_memory(mid)

    def test_hundreds_of_tool_observations_keep_conversation_and_original_exchange(self):
        conversation = self.episode()
        episodes = [conversation] + [self.experience(i) for i in range(250)]
        packed = select_materials(self.repo, episodes, "owner", self.day)
        shown = "\n".join(packed["episodes"] + packed["interaction_evidence"])
        self.assertIn("周末看电影", shown)
        self.assertIn("我今天其实有点委屈", shown)
        self.assertIn("周末的电影我记着呢", shown)
        self.assertEqual(packed["material_selection"]["activity_groups"], 1)
        self.assertLess(shown.count("[经历 "), 5)
        self.assertLessEqual(packed["material_selection"]["selected_bytes"], 24000)
        self.assertEqual(self.repo.count_memories(kinds=["episode"]), 251)

    def test_many_independent_events_remain_bounded_without_dropping_all_activities(self):
        packed = select_materials(self.repo, [self.episode()] +
                                  [self.experience(i, activity=str(i)) for i in range(100)],
                                  "owner", self.day, budget=4000)
        self.assertGreater(packed["material_selection"]["presented_activity_groups"], 0)
        self.assertLessEqual(packed["material_selection"]["selected_bytes"], 4000)

    def test_raw_evidence_is_same_scope_same_day_and_active(self):
        episode = self.episode()
        foreign = self.repo.add_message("local", "qq_group:other", "user", "FOREIGN_SECRET", self.ts,
                                       self.day, actor_id="other")
        old = self.repo.add_message("local", "owner", "user", "OLD_DAY_SECRET", self.ts - 86400,
                                   "2026-01-01", actor_id="owner")
        withdrawn = self.repo.add_message("local", "owner", "user", "WITHDRAWN_SECRET", self.ts,
                                         self.day, actor_id="owner", origin="recalled")
        self.repo.update_memory(episode["id"], meta={"source_message_ids":
                                [*episode["meta"]["source_message_ids"], foreign, old, withdrawn]})
        packed = select_materials(self.repo, [self.repo.get_memory(episode["id"])], "owner", self.day)
        shown = json.dumps(packed, ensure_ascii=False)
        self.assertNotIn("FOREIGN_SECRET", shown)
        self.assertNotIn("OLD_DAY_SECRET", shown)
        self.assertNotIn("WITHDRAWN_SECRET", shown)

    def test_uncertain_outcome_survives_grouping_and_writer_cannot_assume_complete_day(self):
        packed = select_materials(self.repo, [self.experience(1), self.experience(2, status="uncertain"),
                                           self.experience(3)], "owner", self.day)
        self.assertIn("uncertain", "\n".join(packed["episodes"]))
        prompt = build_diary_prompt({"day_key": self.day, **packed}, "人格")
        self.assertIn("未确认和未发送的内容不能写成成功", prompt[1]["content"])
        self.assertIn("材料是有界节选", prompt[1]["content"])

    def test_rewrite_preserves_previous_diary_and_records_voice_and_selection_version(self):
        episode = self.episode()
        signature = self.service._episode_signature([episode])
        old = self.repo.add_memory("diary", "owner", "原日记正文必须保留", day_key=self.day,
                                   meta={**signature, "diary_persona_version": "old-voice"})
        self.service.enqueue_due_diaries()
        claim = self.service.claim_job(["diary"])
        self.assertIsNotNone(claim)
        result = self.service.complete_diary(claim["job"]["id"], "新的当日日记", expected_attempt=claim["job"]["attempts"], attribution_review=approved_diary(claim, '新的当日日记'))
        self.assertEqual(result["diary_id"], old)
        saved = self.repo.get_memory(old)
        self.assertEqual(saved["meta"]["diary_material_policy"], POLICY)
        self.assertEqual(saved["meta"]["diary_persona_version"], signature["diary_persona_version"])
        self.assertTrue(saved["meta"]["diary_material_selection"]["message_versions"])
        previous = self.repo.conn.execute("SELECT content,meta FROM diary_revisions WHERE memory_id=?", (old,)).fetchone()
        self.assertEqual(previous[0], "原日记正文必须保留")
        self.assertEqual(json.loads(previous[1])["diary_persona_version"], "old-voice")
        self.assertEqual(self.service.complete_diary(claim["job"]["id"], "重放")["status"], "ignored")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM diary_revisions").fetchone()[0], 1)

    def test_changed_original_quote_rejects_late_diary_output(self):
        episode = self.episode()
        self.service.enqueue_due_diaries()
        claim = self.service.claim_job(["diary"])
        self.repo.conn.execute("UPDATE messages SET content='修订后的内容' WHERE id=?",
                               (episode["meta"]["source_message_ids"][0],))
        result = self.service.complete_diary(claim["job"]["id"], "包含已失效原话的输出")
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 0)

    def test_worker_uses_the_persona_snapshot_whose_version_was_claimed(self):
        self.episode()
        self.service.enqueue_due_diaries()
        claim = self.service.claim_job(["diary"])
        snapshot = claim["materials"]["diary_persona_prompt"]
        self.assertEqual(hashlib.sha256(snapshot.encode()).hexdigest(),
                         claim["job"]["payload"]["diary_persona_version"])
        sent = []
        def model(messages):
            sent.extend(messages)
            return "记下了今天的电影约定。"
        def changing_voice():
            self.fail("worker must not reload a different persona after claiming")
        MemoryWorker(self.service, model, changing_voice,
                     summary_complete_text=lambda _: '{"verdict":"pass","issues":[]}')._diary(claim["job"], claim["materials"])
        self.assertEqual(sent[0]["content"], snapshot)
        self.assertEqual(self.repo.get_job(claim["job"]["id"])["status"], "done")

    def test_corrected_original_is_annotated_and_stale_fact_cannot_enter_diary(self):
        episode = self.episode("管理员住北京")
        first = episode["meta"]["source_message_ids"][0]
        self.repo.conn.execute("UPDATE messages SET content='我住北京' WHERE id=?", (first,))
        node = self.service.graph.register_memory(episode["id"])
        self.service.correct_memory(scope="owner", node_id=node["id"], operation_id="city-fix",
            expected_digest=node["digest"], kind="fact", quote="北京", replacement="上海", explanation="用户核验的城市纠正")
        while association := self.service.claim_job(["associate"]):
            self.service.complete_association(association["job"]["id"], [])
        packed = select_materials(self.repo, [self.repo.get_memory(episode["id"])], "owner", self.day)
        quote = "\n".join(packed["interaction_evidence"])
        self.assertIn("已核验纠正", quote)
        self.assertIn('"replacement": "上海"', quote)
        self.assertIn("我住北京", quote)  # Original evidence is not silently rewritten.
        self.service.enqueue_due_diaries()
        claim = self.service.claim_job(["diary"])
        self.assertIsNotNone(claim)
        with self.assertRaisesRegex(ValueError, "已纠正"):
            self.service.complete_diary(claim["job"]["id"], "他今天说住北京")
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 0)

    def test_withdrawn_episode_does_not_enter_materials_or_generate_a_diary(self):
        episode = self.episode(messages=False)
        self.repo.update_memory(episode["id"], meta={"withdrawn": True})
        invalid = self.repo.get_memory(episode["id"])
        packed = select_materials(self.repo, [invalid], "owner", self.day)
        self.assertEqual(packed["episodes"], [])
        self.assertEqual(self.service._diary_episodes("owner", self.day), [])
        self.service.enqueue_due_diaries()
        self.assertIsNone(self.service.claim_job(["diary"]))

    def test_active_episode_with_recalled_source_cannot_bypass_missing_graph_node(self):
        episode = self.episode("不可再使用的旧事实")
        self.repo.conn.execute("UPDATE messages SET origin='recalled' WHERE id=?",
                              (episode["meta"]["source_message_ids"][0],))
        self.service.enqueue_due_diaries()
        self.assertIsNone(self.service.claim_job(["diary"]))
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 0)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM consolidation_snapshots").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
