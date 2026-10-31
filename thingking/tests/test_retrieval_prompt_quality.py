"""Synthetic retrieval-to-prompt regressions; no network, models or real memory."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import json
import unittest

from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.lexical_index import LexicalIndex
from memory.channels import local_envelope
from prompt_builder import build_conversation_messages, ContextTokenCounter, _memory_recall_block
from tools.context import ToolContext, tool_context


class RetrievalPromptQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="bines-prompt-quality-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = MemoryRepository(":memory:", clock=lambda: 1000.)
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
                                     MemorySettings(data_dir=Path(self.tmp.name), state_updates=False), clock=lambda: 1000.)
        self.service.sync_identities()

    def context(self, query):
        return self.service.build_context(local_envelope("").to_dict(), query)

    def test_tail_answer_reaches_final_prompt_and_only_shown_spans_are_receipted(self):
        answer = "火星计划的验收码是 ALPHA739。"
        content = "这是一段无关背景。" * 100 + answer
        mid = self.repo.add_memory("episode", "owner", content, conversation_id="other", end_ts=900)
        context = self.context("火星计划验收码")
        self.assertTrue(context["recalled"][0]["matched_spans"])
        journal = Mock()
        with tool_context(ToolContext(journal=journal)):
            messages = build_conversation_messages(context)
        prompt = "\n".join(m["content"] for m in messages)
        self.assertIn(answer, prompt)
        for call in journal.present_memory.call_args_list:
            item, spans = call.args
            if item["id"] == mid:
                self.assertTrue(spans)
                self.assertTrue(all(span in content and span in prompt for span in spans))
                self.assertNotIn(content, spans)

    def test_reason_question_does_not_reintroduce_expired_profile(self):
        for mid, city, when in (("old", "北京", 100), ("current", "上海", 200)):
            self.repo.add_memory("semantic", "owner", "管理员住在" + city, subject_id="owner", memory_id=mid)
            self.repo.temporal.put_fact(mid, "owner", "owner", "residence", city, when)
        current = self.context("为什么喜欢上海")
        self.assertEqual([r["id"] for r in current["profile"]], ["current"])
        historical = self.context("以前住在哪里")
        self.assertEqual({r["id"] for r in historical["profile"]}, {"old", "current"})
        text = "\n".join(m["content"] for m in build_conversation_messages(historical))
        self.assertIn("历史事实，非当前状态", text)
        self.assertIn("有效时间", text)
        self.assertIn("当前有效", text)

    def test_long_source_chunks_do_not_crowd_out_independent_sources(self):
        self.repo.add_memory("episode", "owner", "\n\n".join(["needle " * 10] * 50), memory_id="a-long")
        for i in range(5):
            self.repo.add_memory("episode", "owner", "needle evidence " + str(i) + " padding" * 20)
        hits = self.service.search("needle", ["owner"], k=5)
        self.assertEqual(len(hits), 5)
        self.assertEqual(len({r["id"] for r in hits}), 5)

    def test_expired_candidates_do_not_hide_current_fact(self):
        for i in range(41):
            mid = self.repo.add_memory("semantic", "owner", "needle " * 10 if i < 40 else "needle current " + "padding " * 20)
            self.repo.temporal.put_fact(mid, "owner", "owner", "preference", i, i + 1)
        hits = self.service.search("needle", ["owner"], k=5)
        self.assertEqual([r["id"] for r in hits], [mid])

    def test_vector_overfetch_preserves_old_index_signature(self):
        for i in range(41):
            mid = self.repo.add_memory("semantic", "owner", "unrelated source", memory_id=f"{i:03d}")
            self.repo.temporal.put_fact(mid, "owner", "owner", "preference", i, i + 1)
        requests = []
        def query_keys(query, scopes, kinds, count):
            requests.append(count)
            rows = self.repo.conn.execute("SELECT id FROM retrieval_keys ORDER BY id").fetchall()
            return [(r[0], .1) for r in rows[:count]]
        self.service.retriever.index = type("Index", (), {"query_keys": staticmethod(query_keys)})()
        hits = self.service.search("paraphrase", ["owner"], k=5)
        self.assertEqual([r["id"] for r in hits], [mid])
        self.assertEqual(requests, [40, 80])

    def test_stale_vector_hash_does_not_match_new_source(self):
        mid = self.repo.add_memory("episode", "owner", "unrelated source")
        class Matches(list):
            source_hashes = {f"m:{mid}:0": "old-hash"}
        self.service.retriever.index = type("Index", (), {
            "query_keys": staticmethod(lambda *args: Matches([(f"m:{mid}:0", .1)]))})()
        self.assertEqual(self.service.search("paraphrase", ["owner"]), [])

    def test_stale_search_text_hash_is_rejected_even_with_matching_source_hash(self):
        mid = self.repo.add_memory("episode", "owner", "unrelated source")
        self.service.retriever.sync()
        key = self.service.retriever.keys.get_keys([f"m:{mid}:0"], ["owner"])[f"m:{mid}:0"]
        class Matches(list):
            source_hashes = {key["id"]: key["source_hash"]}
            search_hashes = {key["id"]: "older-search-text"}
        self.service.retriever.index = type("Index", (), {
            "query_keys": staticmethod(lambda *args: Matches([(key["id"], .1)]))})()
        self.assertEqual(self.service.search("paraphrase", ["owner"]), [])

    def test_designated_human_source_stays_first(self):
        self.repo.ensure_conversation("local", "local", "owner")
        source = self.repo.add_message("local", "owner", "user", "我已经戒掉了咖啡", 4, "2026-10-31", actor_id="owner")
        others = [self.repo.add_message("local", "owner", "assistant", "你喜欢咖啡，而且每天喝咖啡", i,
                                       "2026-10-31", actor_id="assistant") for i in range(1, 4)]
        episode = self.repo.add_memory("episode", "owner", "咖啡讨论", meta={"source_message_ids": [source, *others]})
        fact = self.repo.add_memory("semantic", "owner", "管理员不喜欢咖啡", meta={"episode_id": episode,
                                   "source_message_ids": [source], "fact_source_message_id": source})
        evidence = self.service.retriever.keys.source_evidence(fact, ["owner"], "喜欢咖啡", 3)
        self.assertEqual(evidence[0]["source_id"], source)
        self.assertEqual(evidence[0]["role"], "user")

    def test_summary_fallback_is_not_presented_as_user_words(self):
        mid = self.repo.add_memory("episode", "owner", "没有原话的摘要")
        node = self.service.graph.register_memory(mid)
        hit = self.service.graph.view(node, self.service.retriever.keys, "原话")
        block = _memory_recall_block({"recalled": [hit], "recall_query": "原话"})
        self.assertIn("摘要记录:", block)
        self.assertNotIn("对方:", block)

    def test_bounded_sync_preserves_queued_dependency_work(self):
        parent = self.repo.add_memory("episode", "owner", "parent", memory_id="a")
        child = self.repo.add_memory("episode", "owner", "child", memory_id="z", meta={"source_memory_ids": [parent]})
        keys = self.service.retriever.keys
        keys.sync()
        self.repo.update_memory(parent, content="changed")
        keys.sync(limit_sources=1, repair_fts=False)
        dirty = set(tuple(r) for r in self.repo.conn.execute("SELECT source_type,source_id FROM retrieval_dirty_sources"))
        self.assertIn(("memory", child), dirty)
        keys.sync()
        self.assertFalse(list(self.repo.conn.execute("SELECT source_type,source_id FROM retrieval_dirty_sources")))

    def test_vector_maintenance_dispatch_does_not_embed_inline(self):
        maintenance = self.repo.vector_maintenance = Mock()
        maintenance.status.return_value = {"pending": True}
        index = self.service.retriever.index = Mock()
        with patch.object(self.service.retriever.keys, "sync") as synchronize:
            result = self.service.retriever.sync(vectors=True)
        synchronize.assert_not_called()
        maintenance.request.assert_called_once()
        index.upsert_keys.assert_not_called()
        self.assertEqual(result["vector_status"], {"pending": True})

    def test_search_flushes_lexical_sources_while_vector_maintenance_is_queued(self):
        self.repo.vector_maintenance = Mock()
        mid = self.repo.add_memory("episode", "owner", "uniquelysearchable")
        self.service.retriever.sync(vectors=True)
        self.assertEqual([r["id"] for r in self.service.search("uniquelysearchable", ["owner"])], [mid])

    def test_evaluation_probe_counts_key_query_failure(self):
        from verification.memory_retrieval_eval import IndexProbe
        index = Mock()
        index.query_keys.side_effect = RuntimeError("vector failed")
        probe = IndexProbe(index)
        with self.assertRaisesRegex(RuntimeError, "vector failed"):
            probe.query_keys("query", ["owner"], None, 10)
        self.assertEqual(probe.failures, 1)
        self.assertFalse(hasattr(IndexProbe(LexicalIndex(self.repo)), "query_keys"))

    def test_bounded_sync_cycle_finishes_and_later_mutation_requeues_dependents(self):
        self.repo.add_memory("episode", "owner", "left", memory_id="a", meta={"source_memory_ids": ["b"]})
        self.repo.add_memory("episode", "owner", "right", memory_id="b", meta={"source_memory_ids": ["a"]})
        keys = self.service.retriever.keys
        keys.sync()
        self.repo.update_memory("a", content="left changed")
        for _ in range(6):
            keys.sync(limit_sources=1, repair_fts=False)
        self.assertFalse(self.repo.conn.execute("SELECT 1 FROM retrieval_dirty_sources").fetchone())
        self.repo.update_memory("a", content="left changed again")
        keys.sync(limit_sources=1, repair_fts=False)
        self.assertEqual({r[0] for r in self.repo.conn.execute(
            "SELECT source_id FROM retrieval_dirty_sources WHERE source_type='memory'")}, {"b"})

    def test_evaluation_measures_final_prompt_not_just_source_hits(self):
        from verification.memory_retrieval_eval import prompt_quality, current_sources, PROMPT_SOURCE_FILES
        answer = "ALPHA739"
        self.repo.add_memory("episode", "owner", "无关背景。" * 200 + "火星计划验收码是" + answer,
                             conversation_id="other")
        result = prompt_quality(self.service, {"question": "火星计划验收码", "scopes": ["owner"],
                                              "required_terms": [answer]}, 1000.)
        self.assertEqual(result["answer_span_coverage"], 1.)
        self.assertEqual(result["temporal_label_coverage"], 1.)
        self.assertEqual(result["answer_correctness"], "not_evaluated")
        self.assertTrue(set(PROMPT_SOURCE_FILES) <= set(current_sources()))
        with patch.dict("os.environ", {"BINES_CONTEXT_MAX_TOKENS": "256", "BINES_CONTEXT_TOKENIZER_PATH": ""}):
            empty = prompt_quality(self.service, {"question": "火星计划验收码", "scopes": ["owner"],
                                                 "required_terms": [answer]}, 1000.)
        self.assertEqual(empty["answer_span_coverage"], 0.)
        self.assertEqual(empty["presented_item_rate"], 0.)

    def test_bounded_sync_selects_only_the_requested_queue_batch(self):
        keys = self.service.retriever.keys
        keys.sync()
        for i in range(100):
            self.repo.add_memory("episode", "owner", f"batch {i}")
        statements = []
        self.repo.conn.set_trace_callback(statements.append)
        try:
            keys.sync(limit_sources=2, repair_fts=False)
        finally:
            self.repo.conn.set_trace_callback(None)
        selects = [sql for sql in statements if sql.startswith("SELECT source_type,source_id,generation")]
        self.assertEqual(len(selects), 1)
        self.assertIn("LIMIT 2", selects[0])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM retrieval_keys").fetchone()[0], 2)


class PromptBudgetTests(unittest.TestCase):
    def context(self):
        return {"envelope": local_envelope("CURRENT_INPUT_SENTINEL").to_dict(),
                "working": [{"role": "user" if i % 2 == 0 else "assistant", "content": f"record-{i}:" + "长" * 5000, "ts": 1000 + i} for i in range(30)],
                "profile": [{"id": str(i), "kind": "semantic", "content": "画像" * 5000} for i in range(12)],
                "recalled": [{"id": "r", "kind": "episode", "content": "召回" * 5000}],
                "other_channels": [{"role": "user", "content": "跨渠道" * 5000, "ts": 900, "conversation_id": "other"}],
                "state": {"important_thing": "状态" * 5000}, "life": {}}

    @patch.dict("os.environ", {"BINES_CONTEXT_MAX_TOKENS": "32768", "BINES_CONTEXT_TOKENIZER_PATH": ""})
    def test_all_sections_share_real_counted_budget_and_current_input_is_reserved(self):
        context = self.context()
        original = deepcopy(context)
        messages = build_conversation_messages(context)
        counter = ContextTokenCounter()
        diag = context["context_budget"]
        self.assertLessEqual(counter.messages(messages) + diag["current_input_reserve"], 32768)
        self.assertEqual(diag["assembled_tokens"], counter.messages(messages))
        self.assertEqual(diag["counting_method"], "utf8_byte_upper_bound")
        self.assertFalse(diag["over_budget"])
        text = "\n".join(m["content"] for m in messages)
        self.assertIn("record-29", text)
        self.assertNotIn("CURRENT_INPUT_SENTINEL", text)
        self.assertEqual(context["working"], original["working"])
        self.assertEqual(context["profile"], original["profile"])

    @patch.dict("os.environ", {"BINES_CONTEXT_MAX_TOKENS": "256", "BINES_CONTEXT_TOKENIZER_PATH": ""})
    def test_fixed_instructions_are_preserved_and_overflow_is_explicit(self):
        context = self.context()
        journal = Mock()
        with tool_context(ToolContext(journal=journal)):
            messages = build_conversation_messages(context)
        self.assertTrue(context["context_budget"]["over_budget"])
        self.assertIn("--- 行为准则 ---", messages[0]["content"])
        self.assertFalse(any("record-" in m["content"] for m in messages))
        journal.present_memory.assert_not_called()

    def test_local_tokenizer_is_used_without_downloads(self):
        from tokenizers import Tokenizer, models, pre_tokenizers
        with TemporaryDirectory(prefix="bines-tokenizer-") as tmp:
            tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
            tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
            tokenizer.enable_truncation(max_length=1)
            path = Path(tmp) / "tokenizer.json"
            tokenizer.save(str(path))
            with patch.dict("os.environ", {"BINES_CONTEXT_TOKENIZER_PATH": str(path)}):
                counter = ContextTokenCounter()
                self.assertEqual(counter.method, "local_tokenizer")
                self.assertEqual(counter.count("hello world"), 2)

    @patch.dict("os.environ", {"BINES_CONTEXT_MAX_TOKENS": "32768", "BINES_CONTEXT_TOKENIZER_PATH": ""})
    def test_identity_only_envelope_reserves_current_recall_query(self):
        context = self.context()
        context["envelope"]["content"] = ""
        context["recall_query"] = "long-input-" * 4000
        messages = build_conversation_messages(context)
        self.assertGreaterEqual(context["context_budget"]["current_input_reserve"], len(context["recall_query"]))
        self.assertTrue(context["context_budget"]["over_budget"])
        self.assertFalse(any("record-" in m["content"] for m in messages))


if __name__ == "__main__":
    unittest.main()
