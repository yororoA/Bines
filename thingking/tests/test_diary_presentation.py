"""Legacy diary and episode views in conversation and autonomous deliberation."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]

from common.deliberation_context import project_source
from common.memory_presentation import narrative_spans
from prompt_builder import _memory_recall_block


class DiaryPresentationTests(unittest.TestCase):
    def setUp(self):
        self.diary = {
            "kind": "diary", "id": "day-12", "day_key": "2026-11-12",
            "content": "我猜他喜欢去植物园。约在周六三点，这件事记下了。",
            "matched_spans": ["喜欢去植物园", "周六三点"],
            "evidence": [
                {"source_type": "summary", "source_ref": "entry:12", "content": "后来的日记稿"},
                {"source_type": "experience", "source_ref": "run:1", "action": "autonomous_turn",
                 "content": "模型的内心独白"},
                {"source_type": "message", "source_ref": "message:2", "role": "assistant",
                 "content": "感谢你提供的信息，我会持续关注"},
                {"source_type": "message", "source_ref": "message:1", "role": "user",
                 "actor_id": "owner", "content": "那就约在周六三点吧"},
                {"source_type": "experience", "source_ref": "run:2", "action": "read_calendar",
                 "status": "ok", "content": "周六 15:00 见面"},
            ],
        }

    def test_historical_diary_yields_firsthand_facts_in_both_model_inputs(self):
        original = deepcopy(self.diary)
        reply = _memory_recall_block({"recalled": [self.diary], "recall_query": "几时见"}, record=False)
        plan = project_source(self.diary)
        for phrase in ("我猜他喜欢去植物园", "感谢你提供的信息", "模型的内心独白", "后来的日记稿"):
            self.assertNotIn(phrase, reply)
            self.assertNotIn(phrase, str(plan))
        self.assertIn("那就约在周六三点吧", reply)
        self.assertIn("read_calendar", reply)
        self.assertIn("day-12", reply)
        self.assertEqual([e["source_ref"] for e in plan["evidence"]], ["message:1", "run:2"])
        self.assertEqual(plan["id"], self.diary["id"])
        self.assertEqual(self.diary, original)

    def test_original_diary_request_reads_actual_prose_even_with_partial_match(self):
        for query in ("念一下昨天的日记", "请读这篇日记的原文"):
            with self.subTest(query=query):
                reply = _memory_recall_block({"recalled": [self.diary], "recall_query": query}, record=False)
                self.assertIn(self.diary["content"], reply)
                self.assertNotIn("那就约在周六三点吧", reply)
        long = {**self.diary, "content": "开头\n\n" + "中间" * 500 + "日记结尾", "_display_limit": 40}
        reply = _memory_recall_block({"recalled": [long], "recall_query": "念一下日记原文"}, record=False)
        self.assertIn(long["content"][:40], reply)
        self.assertNotIn("日记结尾", reply)

    def test_quote_request_returns_messages_without_generated_monologue_or_diary(self):
        reply = _memory_recall_block({"recalled": [self.diary], "recall_query": "当时说的原话"}, record=False)
        self.assertIn("那就约在周六三点吧", reply)
        self.assertNotIn("模型的内心独白", reply)
        self.assertNotIn("后来的日记稿", reply)
        self.assertNotIn(self.diary["content"], reply)

    def test_partial_matches_respect_sentence_qualifiers_and_source_order(self):
        item = {**self.diary, "evidence": [], "content": "我猜他喜欢去植物园。那天没去植物园。周六三点见。",
                "matched_spans": ["喜欢去植物园", "去植物园。周六三点"]}
        self.assertEqual(narrative_spans(item, "植物园"), ["那天没去植物园。", "周六三点见。"])
        item["matched_spans"] = ["喜欢去植物园"]
        self.assertEqual(narrative_spans(item, "植物园"), [])
        item["content"] += "他自己说喜欢去植物园。"
        self.assertEqual(narrative_spans(item, "植物园"), ["他自己说喜欢去植物园。"])

    def test_source_less_unrelated_diary_keeps_reference_without_old_prose(self):
        item = {**self.diary, "evidence": [], "matched_spans": []}
        reply = _memory_recall_block({"recalled": [item], "recall_query": "修剪指甲"}, record=False)
        self.assertIn("day-12", reply)
        self.assertNotIn("约在周六三点", reply)
        self.assertEqual(project_source(item)["content"], "")

    def test_bridge_prioritizes_topic_under_shared_excerpt_limit(self):
        bridge = {"kind": "episode", "id": "episode-3", "content":
                  "路上买了豆浆。" + "坐车。" * 100 + "修车铺周五才开门。后来下了雨。"}
        prompt = _memory_recall_block({"bridge": bridge, "recall_query": "修车铺哪天开门"}, record=False)
        self.assertIn("修车铺周五才开门。", prompt)
        self.assertNotIn(bridge["content"], prompt)
        spans = narrative_spans(bridge, "修车铺", 12)
        self.assertLessEqual(sum(map(len, spans)), 12)
        self.assertTrue(all(span in bridge["content"] for span in spans))

    def test_turn_episodes_keep_action_status_and_make_reflection_available_on_request(self):
        meta = {"action": "autonomous_turn", "status": "skipped"}
        turn = {"kind": "episode", "id": "turn-12", "origin": "experience", "meta": meta,
                "content": "生成的行动反思", "evidence": [{"source_type": "experience", **meta,
                "source_ref": "run:1", "args": {"reason": "候选说明"}, "content": "当时的行动理由"}]}
        original = deepcopy(turn)
        for slot in ("bridge", "recalled"):
            with self.subTest(slot=slot):
                ctx = {slot: turn if slot == "bridge" else [turn]}
                quiet = _memory_recall_block(ctx, record=False)
                self.assertIn("autonomous_turn", quiet)
                self.assertIn("skipped", quiet)
                self.assertNotIn("生成的行动反思", quiet)
                self.assertNotIn("当时的行动理由", quiet)
                explicit = _memory_recall_block({**ctx, "current_query": "上次为什么没有发消息"}, record=False)
                self.assertIn("生成的行动反思", explicit)
        for kind in ("episode", "semantic"):
            plan = project_source({**turn, "kind": kind})
            self.assertEqual(plan["content"], "")
            self.assertEqual(plan["evidence"][0]["action"], "autonomous_turn")
            self.assertNotIn("当时的行动理由", str(plan))
            self.assertNotIn("候选说明", str(plan))
        self.assertEqual(turn, original)


if __name__ == "__main__":
    unittest.main()
