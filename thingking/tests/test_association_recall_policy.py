"""Deterministic, temporary-DB regressions; no model, network, or live data."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from helpers import Clock
from memory.evidence import EvidenceStore
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.retrieval import HybridRetriever
from memory.service import MemoryService
from memory.settings import MemorySettings


class RecallPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bines_recall_policy_")
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.settings = MemorySettings(data_dir=Path(self.tmp.name), state_updates=False)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def node(self, text, importance=5):
        return self.service.graph.register_memory(self.repo.add_memory("episode", "owner", text, importance=importance))

    def finish_existing(self):
        self.repo.conn.execute("UPDATE jobs SET status='done'")

    def claim(self):
        claim = self.service.claim_job(["associate"])
        self.assertIsNotNone(claim)
        return claim

    def complete(self, claim, target=None, confidence=.8, key="shared-promise", reason="共享的具体约定"):
        plan = claim["job"]["payload"]["association_plan"]
        decisions = []
        for alias, nid in plan["aliases"].items():
            if alias == "s" or alias in plan["cached"]:
                continue
            links = []
            if target and nid == target["id"]:
                evidence = [next(eid for eid, row in plan["catalog"].items() if row["node_id"] == source)
                            for source in (plan["aliases"]["s"], nid)]
                links = [{"reason_key": key, "reason": reason, "confidence": confidence,
                          "relation_type": "support", "direction": "source_to_target", "evidence_ids": evidence}]
            decisions.append({"target": alias, "links": links})
        return self.service.complete_association(claim["job"]["id"], [], .5, decisions=decisions)

    def cold(self, text="预约代号 QZ981", summary="今天聊了一会", scope="owner"):
        conv = "cold:" + scope
        self.repo.ensure_conversation(conv, "local", scope)
        mid = self.repo.add_message(conv, scope, "user", text, self.clock(), "2026-11-01", actor_id="owner")
        eid = self.repo.add_memory("episode", scope, summary, meta={"source_message_ids": [mid]})
        self.repo.conn.execute("UPDATE messages SET episode_id=? WHERE id=?", (eid, mid))
        raw = self.service.graph.register_raw([self.repo.get_message(mid)])
        self.service.graph.register_memory(eid)
        return mid, eid, raw

    def test_tiny_normal_model_link_cannot_displace_direct_match(self):
        target = self.node("shared unrelated high importance material", 10)
        self.finish_existing()
        source = self.node("needle shared original")
        self.assertEqual(self.complete(self.claim(), target, confidence=1e-9)["status"], "done")
        hits = self.service.search("needle", ["owner"], k=1)
        self.assertEqual(hits[0]["node_id"], source["id"])
        self.assertEqual(self.repo.conn.execute("SELECT confidence FROM association_edges").fetchone()[0], 1e-9)

    def test_graph_score_preserves_absolute_activation_not_only_rank(self):
        source, target = self.node("needle original"), self.node("other material")
        graph = self.service.graph
        scores = []
        for activation in (.01, .3):
            with patch.object(graph, "expand", return_value=[(target["id"], activation, [])]):
                hits = self.service.search("needle", ["owner"], k=2)
            scores.append(next(h["score"] for h in hits if h["node_id"] == target["id"]))
        self.assertGreater(scores[1], scores[0] * 3)

    def test_two_pages_cover_twelve_candidates_and_same_job(self):
        targets = [self.node("needle target " + str(i)) for i in range(12)]
        self.finish_existing()
        source = self.node("needle source")
        first = self.claim()
        self.assertEqual(len(first["materials"]["compact"]["candidates"]), 6)
        result = self.complete(first)
        self.assertEqual(result["status"], "continuing_association")
        self.assertFalse(self.service.graph.get(source["id"])["ready"])
        second = self.claim()
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertGreater(second["job"]["attempts"], first["job"]["attempts"])
        a = set(first["job"]["payload"]["association_plan"]["aliases"].values()) - {source["id"]}
        b = set(second["job"]["payload"]["association_plan"]["aliases"].values()) - {source["id"]}
        self.assertFalse(a & b)
        self.assertEqual(a | b, {n["id"] for n in targets})
        self.assertEqual(self.complete(second)["status"], "done")
        saved = self.repo.get_job(first["job"]["id"])
        self.assertEqual(len(saved["payload"]["association_completed"]), 12)
        self.assertTrue(self.service.graph.get(source["id"])["ready"])

    def test_page_advance_does_not_reset_total_model_call_budget(self):
        self.service.settings = replace(self.settings, job_max_model_calls=1)
        for i in range(8):
            self.node("needle target " + str(i))
        self.finish_existing()
        self.node("needle source")
        first = self.claim()
        event = {"call_id": "a" * 32, "status": "started", "started_at": self.clock(), "model": "fake"}
        self.service.record_model_call(first["job"]["id"], event, first["job"]["attempts"])
        self.assertEqual(self.complete(first)["status"], "continuing_association")
        second = self.claim()
        with self.assertRaisesRegex(ValueError, "budget"):
            self.service.record_model_call(second["job"]["id"], {**event, "call_id": "b" * 32}, second["job"]["attempts"])
        saved = self.repo.get_job(first["job"]["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["payload"]["terminal_reason"], "model_call_budget")
        self.assertEqual(len(saved["payload"]["association_completed"]), 6)

    def test_reused_reason_updates_text_fts_and_version_together(self):
        target = self.node("围巾确认老礼物")
        self.finish_existing()
        source = self.node("围巾老礼物记忆")
        self.complete(self.claim(), target, reason="围巾赠送约定")
        graph = self.service.graph
        chain = self.repo.conn.execute("SELECT id FROM association_chains").fetchone()[0]
        before = graph.reason_version(chain, "owner")
        self.node("围巾新承诺")
        self.complete(self.claim(), source, reason="蓝色围巾承诺")
        self.assertEqual(graph.reasons([chain], "owner")[0]["reason"], "蓝色围巾承诺")
        self.assertNotEqual(graph.reason_version(chain, "owner"), before)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_reason_fts WHERE association_reason_fts MATCH '承诺'").fetchone()[0], 1)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_reason_fts WHERE association_reason_fts MATCH '赠送'").fetchone()[0], 0)

    def test_cold_exact_detail_rescued_without_vector_keys_or_promotion(self):
        mid, _, node = self.cold()
        hits = self.service.search("QZ981", ["owner"])
        self.assertEqual(hits[0]["node_id"], node["id"])
        self.assertIn("QZ981", hits[0]["matched_spans"][0])
        keys = self.service.retriever.keys
        self.assertNotIn(f"u:{mid}:0", keys.all_ids())
        self.assertNotIn(f"u:{mid}:0", {k["id"] for k in keys.unindexed_keys()})
        self.assertEqual(self.service.graph.get(node["id"])["retrieval_status"], "cold")
        self.assertEqual(self.service.retriever.last_trace["cold_fallback"], 1)

    def test_cold_identifier_and_quote_allow_natural_question_words(self):
        _, _, node = self.cold()
        self.assertEqual(self.service.search("之前我提到的预约代号QZ981是什么", ["owner"])[0]["node_id"], node["id"])
        self.assertEqual(self.service.search('你还记得我说的“预约代号 QZ981”吗', ["owner"])[0]["node_id"], node["id"])

    def test_cold_scope_time_recall_and_parent_repair_protection(self):
        mid, eid, _ = self.cold()
        self.assertFalse(self.service.search("QZ981", ["other"]))
        self.assertFalse(self.service.search("QZ981", ["owner"], as_of=self.clock() - 1))
        memory = self.repo.get_memory(eid)
        self.repo.update_memory(eid, meta={**memory["meta"], "repair_pending": True})
        self.assertFalse(self.service.search("QZ981", ["owner"]))
        self.repo.update_memory(eid, meta=memory["meta"])
        self.repo.conn.execute("UPDATE messages SET content='[消息已撤回]' WHERE id=?", (mid,))
        self.assertFalse(self.service.search("QZ981", ["owner"]))

    def test_cold_fallback_quota(self):
        for i in range(9):
            self.cold("代号 QZ981 的第" + str(i) + "个原话")
        hits = self.service.search("QZ981", ["owner"], k=10)
        self.assertLessEqual(len(hits), 3)

    def test_exposure_zero_reward_citations_decay_without_harming_prior(self):
        node = self.node("能够核验的原始事实")
        usage = self.service.usage
        presented = {"node_id": node["id"], "node_digest": node["digest"], "spans": ["能够核验的原始事实"]}
        before = usage.prior(node)
        for i in range(100):
            usage.record("owner", "display:" + str(i), [presented], [], "")
        self.assertEqual(usage.prior(node), before)
        usage.record("owner", "cite", [presented], [{"source_id": node["id"], "answer_span": "答案",
                    "source_quote": "原始事实"}], "答案")
        rewarded = usage.prior(node)
        self.assertGreater(rewarded, before)
        self.clock.advance(365 * 86400)
        self.assertLess(usage.prior(node) - before, (rewarded - before) / 10)
        self.assertGreaterEqual(usage.prior(node), before)

    def test_quote_free_and_inferred_overlap_do_not_train_reward(self):
        node = self.node("这是一条长度足够长的可以复用的完整原话")
        shown = {"node_id": node["id"], "node_digest": node["digest"], "spans": ["这是一条长度足够长的可以复用的完整原话"]}
        self.service.usage.record("owner", "overlap", [shown], [{"source_id": node["id"], "answer_span": "原话"}], shown["spans"][0])
        self.assertEqual(self.service.usage.prior(node), .5)

    def test_opportunities_bound_positive_reward_without_negative_labels(self):
        node = self.node("可核验来源")
        shown = {"node_id": node["id"], "node_digest": node["digest"], "spans": ["可核验来源"]}
        usage = self.service.usage
        usage.record("owner", "cited-once", [shown], [{"source_id": node["id"], "answer_span": "答案",
            "source_quote": "可核验来源"}], "答案")
        reward = usage.reward(node["id"])
        for i in range(10):
            usage.record("owner", "unused:" + str(i), [shown], [], "")
        self.assertLess(usage.reward(node["id"]), reward)
        self.assertGreater(usage.reward(node["id"]), 0)
        self.assertNotIn("rejected", usage.counts(node["id"]))

    def test_explicit_exploration_never_evicts_facts_and_default_is_off(self):
        source, weak = self.node("needle original"), self.node("other possible context")
        graph = self.service.graph
        with patch.object(graph, "expand", return_value=[(weak["id"], .0005, [])]):
            hits = self.service.retriever.search("needle", ["owner"], k=2, retrieval_mode="explore")
            self.assertEqual([h["node_id"] for h in hits], [source["id"]])
            self.service.retriever.settings = replace(self.settings, retrieval_exploration_enabled=True)
            hits = self.service.retriever.search("needle", ["owner"], k=1, retrieval_mode="explore")
            self.assertEqual([h["node_id"] for h in hits], [source["id"]])
            hits = self.service.retriever.search("needle", ["owner"], k=2, retrieval_mode="explore")
            self.assertEqual(hits[1]["retrieval_role"], "exploration")

    def test_index_supplied_chunks_and_context_fitting_are_used(self):
        class TokenIndex:
            chunking_signature = "test-token-limit-4"
            def split_text(self, text):
                return [text[i:i+4] for i in range(0, len(text), 4)]
            def fit_text(self, text):
                return text[:6]
        node = self.node("abcdefghij")
        keys = EvidenceStore(self.repo, index=TokenIndex())
        keys.sync()
        rows = [r for r in keys.unindexed_keys() if r["memory_id"] == node["memory_id"]]
        self.assertEqual([r["content"] for r in rows], ["abcd", "efgh", "ij"])
        self.assertTrue(all(len(r["search_text"]) <= 6 for r in rows))
        self.assertEqual(keys.vector_pending_count(), 3)

    def test_embedding_context_fit_does_not_remove_lexical_alias(self):
        class TinyIndex:
            persistent = False
            chunking_signature = "tiny-context-limit"
            def split_text(self, text):
                return [text]
            def fit_text(self, text):
                return text[:8]
        node = self.node("原始事实")
        self.repo.update_memory(node["memory_id"], meta={"aliases": ["head " * 100 + "tailalias"]})
        self.service.graph.register_memory(node["memory_id"])
        retriever = HybridRetriever(self.repo, TinyIndex(), self.settings)
        hits = retriever.search("tailalias", ["owner"], expand_graph=False)
        self.assertEqual(hits[0]["memory_id"], node["memory_id"])
        row = self.repo.conn.execute("SELECT content,search_text,tokens FROM retrieval_keys WHERE memory_id=?", (node["memory_id"],)).fetchone()
        self.assertEqual(row["content"], "原始事实")
        self.assertNotIn("tailalias", row["search_text"])
        self.assertIn("tailalias", row["tokens"])

    def test_empty_query_does_not_reuse_previous_trace(self):
        self.node("needle fact")
        self.service.search("needle", ["owner"])
        self.assertFalse(self.service.search("", ["owner"]))
        self.assertEqual(self.service.retriever.last_trace["candidates"], 0)
        self.assertEqual(self.service.retriever.last_trace["status"], "empty_query")

    def test_bounded_dirty_batch_processes_oldest_generation_first(self):
        nodes = sorted([self.node("old batch"), self.node("new batch")], key=lambda n: n["memory_id"])
        newer, older = nodes
        keys = self.service.retriever.keys
        keys.sync()
        self.repo.conn.execute("DELETE FROM retrieval_dirty_sources")
        self.repo.conn.execute("DELETE FROM retrieval_propagation_marks")
        self.repo.conn.execute("INSERT INTO retrieval_dirty_sources VALUES('memory',?,2)", (newer["memory_id"],))
        self.repo.conn.execute("INSERT INTO retrieval_dirty_sources VALUES('memory',?,1)", (older["memory_id"],))
        with patch.object(keys, "_source_keys", wraps=keys._source_keys) as build:
            keys.sync(limit_sources=1, repair_fts=False)
        self.assertEqual([c.args[0]["id"] for c in build.call_args_list], [older["memory_id"]])
        self.assertIsNotNone(self.repo.conn.execute("SELECT 1 FROM retrieval_dirty_sources WHERE source_id=?",
                                                  (newer["memory_id"],)).fetchone())


if __name__ == "__main__":
    unittest.main()
