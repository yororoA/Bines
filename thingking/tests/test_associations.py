"""Isolated lifecycle and graph regression; no model downloads or production DB."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import Clock, local_env, qq_group_env
from memory.associations import JOB_ASSOCIATE
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.worker import MemoryWorker


class AssociationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bines_associations_")
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.settings = MemorySettings(data_dir=Path(self.tmp.name), state_updates=False,
                                       summarize_trigger=8, summarize_chunk=8,
                                       retention_enabled=True)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def raw(self, text, envelope=None):
        result = self.service.record_turn(envelope or local_env(self.clock() - 86400),
                                          text, "答应了：" + text)
        return self.service.graph.for_message(result["message_ids"][0])

    def claim(self, kind):
        claimed = self.service.claim_job([kind])
        self.assertIsNotNone(claimed)
        return claimed

    def drain_associations(self, priors=()):
        jobs = []
        while claimed := self.service.claim_job([JOB_ASSOCIATE]):
            prior = priors[len(jobs)] if len(jobs) < len(priors) else .5
            self.service.complete_association(claimed["job"]["id"], [], prior)
            jobs.append(claimed)
        return jobs

    def link(self, claimed, target, reason_key="promise:scarf", relation="support", confidence=.8):
        source = claimed["materials"]["source"]
        candidate = next(c for c in claimed["materials"]["candidates"] if c["id"] == target["id"])
        return {"target_id": target["id"], "reason_key": reason_key, "reason": "蓝色围巾的明确赠送约定",
                "relation_type": relation, "confidence": confidence,
                "evidence": [{"node_id": source["id"], "quote": source["content"].splitlines()[0]},
                             {"node_id": candidate["id"], "quote": candidate["content"].splitlines()[0]}]}

    def test_top3_frozen_at_start_and_all_sources_consolidated(self):
        nodes = [self.raw("蓝色围巾约定" + letter) for letter in "abcd"]
        self.drain_associations([.7, .3, .9, .8])
        claimed = self.claim("summarize")
        snapshot = claimed["materials"]["retention"]
        self.assertEqual(snapshot["data"]["protected"], [nodes[i]["id"] for i in (2, 3, 0)])
        # A later graph change must not move this decision.
        self.repo.conn.execute("UPDATE association_nodes SET prior=.95 WHERE id=?", (nodes[1]["id"],))
        result = self.service.complete_summary(claimed["job"]["id"], "四个约定全部摘要")
        self.assertEqual(set(result["protected_raw"]), {nodes[i]["id"] for i in (2, 3, 0)})
        self.assertEqual(len(self.repo.get_memory(result["episode_id"])["meta"]["source_message_ids"]), 8)
        self.assertEqual(self.repo.pending_message_count("local"), 0)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM messages WHERE episode_id IS NOT NULL").fetchone()[0], 8)
        self.assertEqual(self.service.graph.get(nodes[1]["id"])["activation_factor"], .1)
        hits = self.service.search("蓝色围巾", ["owner"])
        self.assertEqual(next(h for h in hits if h.get("node_id") == nodes[1]["id"])["retrieval_status"], "cold")
        self.service.reconcile_index()
        self.assertEqual(self.service.graph.get(nodes[1]["id"])["retrieval_status"], "cold")

    def test_failed_generation_and_retry_keep_snapshot(self):
        for i in range(4):
            self.raw(f"蓝色围巾{i}")
        self.drain_associations([.7, .3, .9, .8])
        first = self.claim("summarize")
        with self.assertRaises(ValueError):
            self.service.complete_summary(first["job"]["id"], "")
        self.service.fail_job(first["job"]["id"], "model timeout")
        self.assertTrue(all(row[0] == "hot" for row in self.repo.conn.execute(
            "SELECT retrieval_status FROM association_nodes WHERE source_type='raw'")))
        self.repo.conn.execute("UPDATE association_nodes SET prior=.95")
        self.clock.advance(61)
        second = self.claim("summarize")
        self.assertEqual(first["materials"]["retention"], second["materials"]["retention"])
        result = self.service.complete_summary(second["job"]["id"], "完整摘要")
        self.assertEqual(self.service.complete_summary(second["job"]["id"], "重复摘要")["status"], "ignored")
        self.assertEqual(self.repo.count_memories(kinds=["episode"]), 1)
        self.assertIsNotNone(result["episode_id"])

    def test_source_edit_rejects_old_model_result(self):
        nodes = [self.raw("围巾约定" + str(i)) for i in range(4)]
        self.drain_associations()
        claimed = self.claim("summarize")
        self.repo.conn.execute("UPDATE messages SET content='修正了约定' WHERE id=?", (nodes[0]["message_ids"][0],))
        result = self.service.complete_summary(claimed["job"]["id"], "旧材料生成的摘要")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.repo.count_memories(), 0)
        self.assertEqual(self.repo.pending_message_count("local"), 8)
        self.assertIsNone(self.service.claim_job(["summarize"]))  # refreshed source must associate first
        self.drain_associations()
        fresh = self.claim("summarize")
        self.assertNotEqual(fresh["materials"]["retention"]["source_hash"],
                            claimed["materials"]["retention"]["source_hash"])
        self.assertEqual(self.service.complete_summary(fresh["job"]["id"], "修订后的完整摘要")["status"], "done")

    def test_diary_top1_keeps_full_signature_and_cold_evidence(self):
        ids = [self.service.add_memory("episode", "owner", f"围巾经历{i}", day_key="2026-10-22")["id"]
               for i in range(3)]
        self.drain_associations([.3, .9, .6])
        self.service.enqueue_due_diaries()
        claimed = self.claim("diary")
        self.assertEqual(len(claimed["materials"]["episodes"]), 3)
        result = self.service.complete_diary(claimed["job"]["id"], "当天全部三段经历")
        diary = self.repo.get_memory(result["diary_id"])
        self.assertEqual(set(diary["meta"]["episode_ids"]), set(ids))
        self.assertEqual([self.service.graph.for_memory(mid)["retrieval_status"] for mid in ids],
                         ["cold", "hot", "cold"])
        self.assertTrue(all(not self.repo.get_memory(mid)["deleted"] for mid in ids))
        self.assertEqual(self.service.enqueue_due_diaries(), 0)
        self.service.reconcile_index()
        self.assertEqual(self.service.enqueue_due_diaries(), 0)

    def test_diary_new_source_invalidates_generation(self):
        self.service.add_memory("episode", "owner", "围巾甲", day_key="2026-10-22")
        self.drain_associations()
        self.service.enqueue_due_diaries()
        claimed = self.claim("diary")
        self.service.add_memory("episode", "owner", "围巾乙", day_key="2026-10-22")
        self.assertEqual(self.service.complete_diary(claimed["job"]["id"], "漏掉乙")["status"], "ignored")
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 0)

    def test_association_updates_chain_and_not_other_reason(self):
        a = self.raw("蓝色围巾赠送甲")
        b = self.raw("蓝色围巾赠送乙")
        first = self.claim(JOB_ASSOCIATE)
        target = b if first["materials"]["source"]["id"] == a["id"] else a
        link = self.link(first, target)
        self.service.complete_association(first["job"]["id"], [link], .5)
        cid = self.repo.conn.execute("SELECT id FROM association_chains").fetchone()[0]
        before = self.service.graph.weights(cid, "owner")[a["id"]]["recall_strength"]
        second = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(second["job"]["id"], [], .5)
        c = self.raw("蓝色围巾赠送丙")
        third = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(third["job"]["id"], [self.link(third, a)], .5)
        weights = self.service.graph.weights(cid, "owner")
        self.assertGreater(weights[a["id"]]["recall_strength"], before)
        self.assertEqual(set(weights), {a["id"], b["id"], c["id"]})
        self.assertTrue(all(w["scored_revision"] == self.repo.conn.execute(
            "SELECT revision FROM association_chains WHERE id=?", (cid,)).fetchone()[0] for w in weights.values()))
        # Query activation cannot change persistent strengths.
        persisted = dict(weights[a["id"]])
        self.service.search("围巾", ["owner"])
        self.assertEqual(self.service.graph.weights(cid, "owner")[a["id"]], persisted)

    def test_scope_and_self_links_rejected_atomically(self):
        a = self.raw("蓝色围巾私密内容")
        secret = self.raw("蓝色围巾群聊内容", qq_group_env("20002", ts=self.clock() - 86400))
        claimed = self.claim(JOB_ASSOCIATE)
        source = claimed["materials"]["source"]["id"]
        self.assertNotIn(secret["id"], [c["id"] for c in claimed["materials"]["candidates"]])
        for target_id in (source, secret["id"]):
            with self.assertRaises(ValueError):
                self.service.complete_association(claimed["job"]["id"],
                    [{"target_id": target_id, "confidence": .8}], .9)
        self.assertEqual(self.service.graph.get(a["id"])["prior"], .5)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 0)

    def test_same_source_summary_cannot_reinforce_raw(self):
        raw = self.raw("蓝色围巾原文")
        mid = self.repo.add_memory("episode", "owner", "蓝色围巾摘要",
                                   meta={"source_message_ids": raw["message_ids"]})
        summary = self.service.graph.register_memory(mid)
        claimed = self.claim(JOB_ASSOCIATE)
        self.assertNotIn(summary["id"], [c["id"] for c in claimed["materials"]["candidates"]])
        self.service.complete_association(claimed["job"]["id"], [], .5)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 0)

    def test_wrong_quote_and_candidate_revision_rejected(self):
        a, b = self.raw("蓝色围巾甲"), self.raw("蓝色围巾乙")
        claimed = self.claim(JOB_ASSOCIATE)
        link = self.link(claimed, b)
        link["evidence"][1]["quote"] = "捏造的原话"
        with self.assertRaises(ValueError):
            self.service.complete_association(claimed["job"]["id"], [link])
        link = self.link(claimed, b)
        self.repo.conn.execute("UPDATE messages SET content='来源正文改动' WHERE id=?", (b["message_ids"][0],))
        self.assertEqual(self.service.complete_association(claimed["job"]["id"], [link])["status"], "stale")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 0)

    def test_association_retry_exhaustion_does_not_block_summary(self):
        for i in range(4):
            self.raw("蓝色围巾" + str(i))
        while claimed := self.service.claim_job([JOB_ASSOCIATE]):
            self.repo.update_job_payload(claimed["job"]["id"],
                {**claimed["job"]["payload"], "generation_failures": 4})
            self.assertEqual(self.service.fail_job(claimed["job"]["id"], "模型不可用")["status"], "failed")
        claimed = self.claim("summarize")
        self.assertEqual(len(claimed["materials"]["retention"]["data"]["association_incomplete"]), 4)

    def test_backoff_dependencies_defer_without_spending_generation_budget(self):
        for i in range(4):
            self.raw("蓝色围巾" + str(i))
        claimed = self.claim(JOB_ASSOCIATE)
        self.service.fail_job(claimed["job"]["id"], "temporary")
        self.assertIsNone(self.service.claim_job(["summarize"]))
        summary = self.repo.conn.execute("""SELECT attempts,status,
            COALESCE(json_extract(payload,'$.generation_failures'),0) FROM jobs WHERE kind='summarize'""").fetchone()
        self.assertEqual(tuple(summary), (1, "pending", 0))  # Lease tokens are never reused.

    def test_complete_turn_not_cut_by_message_chunk(self):
        settings = MemorySettings(data_dir=Path(self.tmp.name), state_updates=False, associations_enabled=False,
                                  summarize_trigger=2, summarize_chunk=1, retention_enabled=True)
        service = MemoryService(self.repo, LexicalIndex(self.repo), settings, self.clock)
        service.record_turn(local_env(self.clock() - 86400), "完整回合用户", "完整回合助手")
        claimed = service.claim_job(["summarize"])
        self.assertEqual(len(claimed["materials"]["messages"]), 2)

    def test_open_runtime_turn_defers_until_finished(self):
        env = local_env(self.clock() - 86400)
        self.service.record_message(env, "user", "围巾用户", source_ref="u", turn_ref="t")
        node = self.service.graph.for_message(1)
        self.assertIsNone(self.service.claim_job([JOB_ASSOCIATE]))
        self.service.record_message(env, "assistant", "围巾助手", source_ref="a", turn_ref="t")
        self.service.record_experience(env, "t", {
            "category": "turn", "action": "dialogue_turn", "status": "completed",
            "source_message_refs": ["u", "a"], "result": "围巾助手", "turn_id": "t"})
        self.drain_associations()
        current = self.service.graph.for_message(1)
        self.assertEqual(current["id"], node["id"])
        self.assertEqual(current["message_ids"], [1, 2])
        self.assertEqual(current["waiting_until"], 0)

    def test_usage_deduplicated_and_only_visible_citations_count(self):
        node = self.raw("蓝色围巾是你的生日礼物")
        presentation = {"node_id": node["id"], "node_digest": node["digest"], "spans": ["蓝色围巾是你的生日礼物"]}
        citation = {"source_id": node["id"], "answer_span": "你说的是蓝色围巾", "source_quote": "蓝色围巾"}
        result = self.service.usage.record("owner", "turn1", [presentation, presentation], [citation, citation],
                                           "你说的是蓝色围巾")
        self.assertEqual(result, {"presented": 1, "cited": 1})
        self.assertEqual(self.service.usage.record("owner", "turn1", [presentation], [citation],
                                                  "你说的是蓝色围巾"), {"presented": 0, "cited": 0})
        self.assertEqual(self.service.usage.record("qq_group:30003", "turn2", [presentation], [citation],
                                                  "你说的是蓝色围巾"), {"presented": 0, "cited": 0})
        self.assertEqual(self.service.usage.record("owner", "turn3", [], [citation],
                                                  "你说的是蓝色围巾")["cited"], 0)
        citation["answer_span"] = "最终回答中没有这句话"
        self.assertEqual(self.service.usage.record("owner", "turn4", [presentation], [citation],
                                                  "你说的是蓝色围巾")["cited"], 0)

    def test_search_candidates_do_not_count_as_usage(self):
        self.raw("蓝色围巾")
        self.service.search("围巾", ["owner"])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_usage_events").fetchone()[0], 0)

    def test_budget_exempts_pending_and_running_sources(self):
        nodes = [self.raw("围巾" + str(i)) for i in range(4)]
        self.drain_associations()
        claimed = self.claim("summarize")
        self.repo.conn.execute("UPDATE association_nodes SET consolidated_into='old'")
        self.service.retention.settings = MemorySettings(raw_hot_budget=0, retention_enabled=True)
        self.assertEqual(self.service.retention.enforce_budget("owner"), 0)
        self.assertTrue(all(self.service.graph.get(n["id"])["retrieval_status"] == "hot" for n in nodes))
        self.repo.finish_job(claimed["job"]["id"])
        self.assertEqual(self.service.retention.enforce_budget("owner"), 4)
        pending = self.raw("待摘要围巾")
        self.service.retention.enforce_budget("owner")
        self.assertEqual(self.service.graph.get(pending["id"])["retrieval_status"], "hot")

    def test_worker_association_real_dispatch_and_idempotency(self):
        self.raw("蓝色围巾")
        self.raw("蓝色围巾赠送约定")
        class Client:
            def claim_job(inner, **params):
                return self.service.claim_job(**params)
            def complete_association(inner, *args, **claim):
                return self.service.complete_association(*args, **claim)
            def fail_job(inner, *args, **claim):
                return self.service.fail_job(*args, **claim)
        def model(messages):
            materials = json.loads(messages[1]["content"])
            return json.dumps({"prior": .7, "decisions": [
                {"target": card["id"], "links": []} for card in materials["candidates"]]})
        worker = MemoryWorker(Client(), model, lambda: "")
        self.assertTrue(worker.run_once())
        self.assertEqual(self.repo.conn.execute("SELECT ready,prior FROM association_nodes").fetchone()[1], .7)
        self.repo.conn.execute("UPDATE jobs SET status='done' WHERE kind='summarize'")
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())

    def test_prompt_budget_records_only_presented_spans(self):
        import prompt_builder
        from common.memory_journal import MemoryJournal, MemoryOutbox
        from tools.context import ToolContext, tool_context
        nodes = [self.raw("围巾" + str(i) + "x" * 900) for i in range(4)]
        views = [self.service.graph.view(n, self.service.retriever.keys) for n in nodes]
        outbox = MemoryOutbox(Path(self.tmp.name) / "outbox.db")
        journal = MemoryJournal(outbox, local_env(), "prompt-turn")
        with tool_context(ToolContext(journal=journal)):
            block = prompt_builder._memory_recall_block({"recalled": views * 10})
        self.assertIn("来源 n:", block)
        self.assertGreater(len(journal.memory_presentations), 0)
        self.assertLess(len(block), 4800)
        self.assertTrue(all(len(span) <= 480 for item in journal.memory_presentations.values() for span in item["spans"]))

    def test_vector_failure_keeps_lexical_and_graph(self):
        self.raw("蓝色围巾")
        class BrokenVector(LexicalIndex):
            def query_keys(inner, *args):
                raise RuntimeError("offline")
        self.service.retriever.index = BrokenVector(self.repo)
        with self.assertLogs("bines.memory.retrieval", "ERROR"):
            hits = self.service.search("蓝色围巾", ["owner"])
        self.assertTrue(hits)
        self.assertTrue(hits[0]["evidence"])

    def test_cold_neighbor_recalled_and_withdrawal_wins(self):
        a, b = self.raw("星河蓝色围巾"), self.raw("纸鹤围巾约定")
        claimed = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(claimed["job"]["id"], [self.link(claimed, b)], .5)
        self.drain_associations()
        self.repo.conn.execute("UPDATE association_nodes SET retrieval_status='cold',activation_factor=.1 WHERE id=?",
                               (b["id"],))
        self.service.retriever.sync()
        hits = self.service.search("星河", ["owner"])
        self.assertIn(b["id"], {h["node_id"] for h in hits})
        cold = next(h for h in hits if h["node_id"] == b["id"])
        self.assertEqual(cold["retrieval_status"], "cold")
        self.assertGreater(cold["graph_activation"], 0)
        self.repo.conn.execute("UPDATE messages SET origin='recalled',content='[消息已撤回]' WHERE id=?",
                               (b["message_ids"][0],))
        self.assertNotIn(b["id"], {h["node_id"] for h in self.service.search("星河", ["owner"])})

    def test_reason_only_query_activates_scoped_graph(self):
        a, b = self.raw("星河约定甲"), self.raw("星河约定乙")
        claimed = self.claim(JOB_ASSOCIATE)
        link = self.link(claimed, b)
        link["reason"] = "小熊礼物的赠送约定"
        self.service.complete_association(claimed["job"]["id"], [link], .5)
        self.drain_associations()
        self.assertTrue(self.service.search("小熊", ["owner"]))
        self.assertEqual(self.service.search("小熊", ["qq_group:30003"]), [])

    def test_contradiction_does_not_raise_support_strength(self):
        a, b = self.raw("蓝色围巾甲"), self.raw("蓝色围巾乙")
        claimed = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(claimed["job"]["id"], [self.link(claimed, b, relation="contradicts")], .5)
        self.drain_associations()
        cid = self.repo.conn.execute("SELECT id FROM association_chains").fetchone()[0]
        self.assertAlmostEqual(self.service.graph.weights(cid, "owner")[a["id"]]["recall_strength"], .5)

    def test_duplicate_commit_cannot_add_evidence_strength(self):
        a, b = self.raw("蓝色围巾甲"), self.raw("蓝色围巾乙")
        claimed = self.claim(JOB_ASSOCIATE)
        link = self.link(claimed, b)
        self.service.complete_association(claimed["job"]["id"], [link], .5)
        cid = self.repo.conn.execute("SELECT id FROM association_chains").fetchone()[0]
        before = self.service.graph.weights(cid, "owner")[a["id"]]["recall_strength"]
        self.assertEqual(self.service.complete_association(claimed["job"]["id"], [link], .5)["status"], "ignored")
        self.assertEqual(self.service.graph.weights(cid, "owner")[a["id"]]["recall_strength"], before)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 1)

    def test_presentation_alone_does_not_change_strength(self):
        a, b = self.raw("蓝色围巾甲"), self.raw("蓝色围巾乙")
        claimed = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(claimed["job"]["id"], [self.link(claimed, b)], .5)
        self.drain_associations()
        cid = self.repo.conn.execute("SELECT id FROM association_chains").fetchone()[0]
        before = self.service.graph.weights(cid, "owner")[a["id"]]
        presented = {"node_id": a["id"], "node_digest": a["digest"], "spans": ["蓝色围巾甲"], "chain_ids": [cid]}
        self.service.usage.record("owner", "visible", [presented], [], "")
        after = self.service.graph.weights(cid, "owner")[a["id"]]
        self.assertEqual(after["recall_strength"], before["recall_strength"])
        self.assertEqual(after["citation_strength"], before["citation_strength"])

    def test_old_data_backfill_advances_past_invalid_rows(self):
        for i in range(5):
            self.repo.add_memory("episode", "owner", f"审计内容{i}", meta={"audit_only": True})
        last = self.repo.add_memory("episode", "owner", "可用的蓝色围巾记忆")
        for _ in range(4):
            self.service.graph.backfill(limit=2)
        self.assertIsNotNone(self.service.graph.for_memory(last))

    def test_different_reason_strengths_do_not_compete_persistently(self):
        a, b, c = (self.raw("蓝色围巾" + letter) for letter in "abc")
        first = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(first["job"]["id"], [self.link(first, b, reason_key="gift")], .5)
        second = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(second["job"]["id"], [], .5)
        gift = self.repo.conn.execute("SELECT id FROM association_chains WHERE reason_key='gift'").fetchone()[0]
        before = self.service.graph.weights(gift, "owner")[a["id"]]["recall_strength"]
        third = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(third["job"]["id"], [self.link(third, a, reason_key="preference")], .5)
        self.assertEqual(self.service.graph.weights(gift, "owner")[a["id"]]["recall_strength"], before)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM chain_memberships WHERE node_id=?",
                                               (a["id"],)).fetchone()[0], 2)

    def test_runtime_journal_commits_visible_and_actual_citation_only(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        from tools.context import ToolContext, tool_context
        from tools.memory_tool import search_memory, cite_memory
        from memory.server import dispatch
        node = self.raw("蓝色围巾是生日礼物")
        outbox = MemoryOutbox(Path(self.tmp.name) / "usage-outbox.db")
        journal = MemoryJournal(outbox, local_env(self.clock()), "actual-turn")
        journal.source_message_refs = ["actual-user", "actual-assistant"]
        journal.turn_message_refs = list(journal.source_message_refs)
        outbox.record_message(journal.envelope, "user", "记得那件生日礼物吗", "actual-user")
        outbox.record_message(journal.envelope, "assistant", "那是蓝色围巾", "actual-assistant")
        with tool_context(ToolContext(journal=journal)), patch("tools.memory_tool.deps.memory_client", self.service):
            hits = search_memory("生日礼物")
            journal.confirm_memory_presentations([{"content": str(hits)}])
            cite_memory(hits[0]["citation_id"], "那是蓝色围巾", "蓝色围巾")
            journal.turn_result = "那是蓝色围巾"
        def call(method, **params):
            response = dispatch(self.service, {"method": method, "params": params})
            self.assertTrue(response["ok"], response)
        outbox.sync(call)
        self.assertEqual(self.service.usage.counts(node["id"]), {"presented": 1, "cited": 1})
        outbox.sync(call)
        self.assertEqual(self.service.usage.counts(node["id"]), {"presented": 1, "cited": 1})

    def test_unsubmitted_prompt_does_not_grant_usage(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        from tools.context import ToolContext, tool_context
        node = self.raw("蓝色围巾")
        outbox = MemoryOutbox(Path(self.tmp.name) / "unsubmitted.db")
        journal = MemoryJournal(outbox, local_env(), "unsubmitted")
        item = {"node_id": node["id"], "node_digest": node["digest"]}
        with tool_context(ToolContext(journal=journal)):
            journal.present_memory(item, ["蓝色围巾"])
            with self.assertRaises(ValueError):
                journal.cite_memory(node["id"], "这是蓝色围巾")
            journal.confirm_memory_presentations([{"content": node["id"]}])
            self.assertEqual(journal.confirmed_memory_presentations, {})
        with outbox.connection() as db:
            params = json.loads(db.execute("SELECT params FROM memory_outbox WHERE source_ref=?",
                                          (journal.prefix + ":turn",)).fetchone()[0])
        self.assertEqual(params["record"]["memory_usage"]["presented"], [])

    def test_completed_model_request_confirms_actual_source_spans(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        from tools.context import ToolContext, tool_context
        from thinking_model_helper import ThinkingModelHelper
        from unittest.mock import Mock
        from types import SimpleNamespace
        node = self.raw("蓝色围巾")
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "submitted.db"), local_env(), "submitted")
        helper = ThinkingModelHelper.__new__(ThinkingModelHelper)
        helper.model, helper.client = "test", Mock()
        helper.client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content="这是蓝色围巾"), finish_reason="stop")])
        with tool_context(ToolContext(journal=journal)):
            journal.present_memory({"node_id": node["id"], "node_digest": node["digest"]}, ["蓝色围巾"])
            helper.complete_text([{"role": "user", "content": f"来源 {node['id']}：蓝色围巾"}])
            self.assertIn(node["id"], journal.confirmed_memory_presentations)
            journal.cite_memory(node["id"], "这是蓝色围巾", "蓝色围巾")

    def test_quoted_history_is_dependency_not_part_of_current_raw_turn(self):
        env = local_env(self.clock())
        self.service.record_message(env, "user", "旧围巾原话", source_ref="old-quote")
        old = self.service.graph.for_message(1)
        self.service.record_message(env, "user", "请看引用", source_ref="u", turn_ref="current-turn")
        self.service.record_message(env, "assistant", "看到了", source_ref="a", turn_ref="current-turn")
        self.service.record_experience(env, "current-turn", {
            "category": "turn", "action": "dialogue_turn", "status": "completed", "result": "看到了",
            "source_message_refs": ["old-quote", "u", "a"], "turn_message_refs": ["u", "a"]})
        self.assertEqual(self.service.graph.for_message(1)["id"], old["id"])
        self.assertEqual(self.service.graph.for_message(2)["message_ids"], [2, 3])

    def test_raw_group_revision_detaches_residual_messages(self):
        for i in range(3):
            self.service.record_message(local_env(self.clock()), "user", f"围巾{i}", source_ref=str(i))
        group = self.service.graph.register_raw([self.repo.get_message(i) for i in (1, 2, 3)], "batch")
        self.service.graph.register_raw([self.repo.get_message(i) for i in (1, 2)], "batch")
        self.assertIsNone(self.service.graph.for_message(3))
        self.service.graph.backfill()
        self.assertNotEqual(self.service.graph.for_message(3)["id"], group["id"])
        self.assertEqual(self.service.graph.for_message(3)["message_ids"], [3])

    def test_query_cache_reuses_reads_and_invalidates_on_source_change(self):
        node = self.raw("缓存中的蓝色围巾")
        graph = self.service.graph
        with patch.object(self.repo, "get_message", wraps=self.repo.get_message) as read:
            with graph.reads.session():
                self.assertTrue(graph.valid(node))
                self.assertTrue(graph.valid(node))
                graph.material(node)
                self.assertEqual(read.call_count, 2)
                self.repo.conn.execute("UPDATE messages SET content='修改后的来源' WHERE id=?",
                                       (node["message_ids"][0],))
                self.assertFalse(graph.valid(node))
                self.assertGreater(read.call_count, 2)

    def test_query_cache_detects_external_withdrawal(self):
        import sqlite3
        path = Path(self.tmp.name) / "external.db"
        repo = MemoryRepository(path, self.clock)
        peer = sqlite3.connect(path, isolation_level=None)
        try:
            service = MemoryService(repo, LexicalIndex(repo), self.settings, self.clock)
            result = service.record_turn(local_env(self.clock()), "云杉围巾", "记住了")
            node = service.graph.for_message(result["message_ids"][0])
            with service.graph.reads.session():
                self.assertTrue(service.graph.valid(node))
                changes = repo.conn.total_changes
                peer.execute("UPDATE messages SET origin='recalled',content='[消息已撤回]' WHERE id=?",
                             (result["message_ids"][0],))
                self.assertEqual(repo.conn.total_changes, changes)
                self.assertFalse(service.graph.valid(node))
        finally:
            peer.close()
            repo.close()

    def test_cold_summary_resolves_evidence_without_direct_index(self):
        a = self.service.add_memory("episode", "owner", "星河围巾约定")["id"]
        b = self.service.add_memory("episode", "owner", "纸鹤围巾约定")["id"]
        source = self.claim(JOB_ASSOCIATE)
        node = self.service.graph.for_memory(b)
        self.service.complete_association(source["job"]["id"], [self.link(source, node)], .5)
        self.drain_associations()
        self.repo.conn.execute("UPDATE association_nodes SET retrieval_status='cold',activation_factor=.1 WHERE id=?",
                               (node["id"],))
        hits = self.service.search("星河", ["owner"])
        cold = next(hit for hit in hits if hit["id"] == b)
        self.assertTrue(cold["evidence"])
        self.assertEqual(cold["retrieval_status"], "cold")
        self.assertNotIn(f"m:{b}:0", self.service.retriever.keys.all_ids())
        self.assertEqual(self.service.retriever.keys.source_evidence(b, ["qq_group:30003"]), [])
        self.service.delete_memory(b)
        self.assertNotIn(b, {hit["id"] for hit in self.service.search("星河", ["owner"])})

    def test_only_selected_reason_receives_citation_feedback(self):
        a, b = self.raw("蓝色围巾甲"), self.raw("蓝色围巾乙")
        first = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(first["job"]["id"], [self.link(first, b, reason_key="gift")], .5)
        second = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(second["job"]["id"], [self.link(second, a, reason_key="hobby")], .5)
        graph = self.service.graph
        chains = {r["reason_key"]: r["id"] for r in self.repo.conn.execute("SELECT id,reason_key FROM association_chains")}
        gift, hobby = chains["gift"], chains["hobby"]
        before_gift = graph.weights(gift, "owner")[a["id"]]["recall_strength"]
        before_hobby = graph.weights(hobby, "owner")[a["id"]]["recall_strength"]
        presented = {"node_id": a["id"], "node_digest": a["digest"], "spans": ["蓝色围巾甲"],
                     "scope": "owner", "chain_ids": [gift, hobby]}
        self.service.competition.mint("围巾礼物", [presented])
        citation = {"source_id": a["id"], "answer_span": "这件礼物", "chain_id": gift, "source_quote": "蓝色围巾甲"}
        self.service.usage.record("owner", "reason-turn", [presented], [citation], "这件礼物")
        self.assertGreater(graph.weights(gift, "owner")[a["id"]]["recall_strength"], before_gift)
        self.assertEqual(graph.weights(hobby, "owner")[a["id"]]["recall_strength"], before_hobby)
        self.assertEqual(self.service.usage.counts(a["id"], hobby), {})
        self.assertEqual(self.service.usage.counts(a["id"]), {"presented": 1, "cited": 1})

    def test_undisplayed_reason_cannot_receive_citation(self):
        a, b = self.raw("蓝色围巾甲"), self.raw("蓝色围巾乙")
        first = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(first["job"]["id"], [self.link(first, b)], .5)
        self.drain_associations()
        cid = self.repo.conn.execute("SELECT id FROM association_chains").fetchone()[0]
        shown = {"node_id": a["id"], "node_digest": a["digest"], "spans": ["蓝色围巾甲"]}
        result = self.service.usage.record("owner", "hidden-reason", [shown],
                    [{"source_id": a["id"], "answer_span": "围巾", "chain_id": cid}], "围巾")
        self.assertEqual(result, {"presented": 1, "cited": 0})
        self.assertEqual(self.service.usage.counts(a["id"], cid), {})

    def test_evidence_is_materialized_for_selected_results_only(self):
        for i in range(8):
            self.service.add_memory("episode", "owner", f"独立的云杉围巾约定{i}")
        keys = self.service.retriever.keys
        with patch.object(keys, "source_evidence", wraps=keys.source_evidence) as evidence:
            result = self.service.search("云杉围巾", ["owner"], k=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(evidence.call_count, 2)

    def test_old_weight_policy_invalidates_derived_scores_only(self):
        from memory.associations import AssociationStore
        a, b = self.raw("蓝色围巾甲"), self.raw("蓝色围巾乙")
        first = self.claim(JOB_ASSOCIATE)
        self.service.complete_association(first["job"]["id"], [self.link(first, b)], .5)
        self.drain_associations()
        cid = self.repo.conn.execute("SELECT id FROM association_chains").fetchone()[0]
        self.service.graph.weights(cid, "owner")
        self.repo.conn.execute("UPDATE chain_memberships SET recall_strength=.99")
        self.repo.conn.execute("UPDATE meta SET value='old-policy' WHERE key='association_weight_policy'")
        graph = AssociationStore(self.repo, self.settings)
        self.assertEqual(self.repo.conn.execute("SELECT MAX(scored_revision) FROM chain_memberships").fetchone()[0], 0)
        self.assertLess(graph.weights(cid, "owner")[a["id"]]["recall_strength"], .99)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4)

    def test_long_candidate_keeps_matching_tail_and_real_quote(self):
        self.raw("菱叶苔藓的观察结果")
        long_text = "旧记录的普通正文。\n" * 1800 + "菱叶苔藓出现在溪流旁。"
        mid = self.service.add_memory("episode", "owner", long_text)["id"]
        node = self.service.graph.for_memory(mid)
        claimed = self.claim(JOB_ASSOCIATE)
        candidate = next(c for c in claimed["materials"]["candidates"] if c["id"] == node["id"])
        self.assertIn("菱叶苔藓出现在溪流旁。", candidate["content"])
        self.assertLess(len(candidate["content"]), 6000)
        link = self.link(claimed, node, reason_key="moss")
        link["evidence"][1]["quote"] = "菱叶苔藓出现在溪流旁。"
        self.assertEqual(self.service.complete_association(claimed["job"]["id"], [link])["status"], "done")

    def test_candidate_search_does_not_use_speaker_headers(self):
        source = self.raw("菱叶苔藓")
        unrelated = self.raw("银河轨道")
        claimed = self.claim(JOB_ASSOCIATE)
        self.assertEqual(claimed["materials"]["source"]["id"], source["id"])
        self.assertNotIn(unrelated["id"], {c["id"] for c in claimed["materials"]["candidates"]})

    def test_zero_confidence_does_not_activate_reason_or_neighbor(self):
        a, b = self.raw("星河围巾甲"), self.raw("纸鹤围巾乙")
        first = self.claim(JOB_ASSOCIATE)
        link = self.link(first, b, confidence=0)
        link["reason"] = "北极旅行的关联理由"
        self.service.complete_association(first["job"]["id"], [link], .5)
        self.drain_associations()
        self.assertEqual(self.service.search("北极", ["owner"]), [])
        self.assertNotIn(b["id"], {h["node_id"] for h in self.service.search("星河", ["owner"])})

    def test_changed_source_is_not_activated_by_old_reason(self):
        a = self.service.add_memory("episode", "owner", "星河围巾约定")["id"]
        b = self.service.add_memory("episode", "owner", "纸鹤围巾约定")["id"]
        first = self.claim(JOB_ASSOCIATE)
        link = self.link(first, self.service.graph.for_memory(b))
        link["reason"] = "北极旅行的关联理由"
        self.service.complete_association(first["job"]["id"], [link], .5)
        self.drain_associations()
        self.assertTrue(self.service.search("北极", ["owner"]))
        self.service.update_memory(b, content="讨论热咖啡的口感")
        self.assertEqual(self.service.search("北极", ["owner"]), [])
        node = self.service.graph.for_memory(b)
        before = self.service.graph.strength(node)
        self.service.usage.record("owner", "new-topic", [
            {"node_id": node["id"], "node_digest": node["digest"], "spans": ["讨论热咖啡的口感"]}],
            [{"source_id": node["id"], "answer_span": "热咖啡", "source_quote": "热咖啡"}], "热咖啡")
        self.assertGreater(self.service.graph.strength(node), before)

    def test_confirmed_reason_survives_later_request_in_same_turn(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "two-requests.db"), local_env(), "two-requests")
        item = {"node_id": "n:visible", "node_digest": "revision-1", "chain_ids": ["c:gift"]}
        journal.present_memory(item, ["蓝色围巾"])
        journal.confirm_memory_presentations([{"content": "n:visible c:gift 蓝色围巾"}])
        journal.present_memory({**item, "chain_ids": ["c:hobby"]}, ["棉花娃娃"])
        journal.confirm_memory_presentations([{"content": "n:visible c:hobby 棉花娃娃"}])
        journal.cite_memory("n:visible", "礼物是围巾", "蓝色围巾", "c:gift")
        self.assertEqual(set(journal.confirmed_memory_presentations["n:visible"]["chain_ids"]),
                         {"c:gift", "c:hobby"})

    def test_invalid_members_do_not_consume_reason_candidate_budget(self):
        ids = [self.service.add_memory("episode", "owner", f"云杉围巾{i}")["id"] for i in range(12)]
        first = self.claim(JOB_ASSOCIATE)
        links = []
        for mid in ids[1:]:
            link = self.link(first, self.service.graph.for_memory(mid), reason_key="north", confidence=0)
            link["reason"] = "北极旅行的关联理由"
            links.append(link)
        self.service.complete_association(first["job"]["id"], links, .95)
        for _ in range(9):
            job = self.claim(JOB_ASSOCIATE)
            self.service.complete_association(job["job"]["id"], [], .9)
        job = self.claim(JOB_ASSOCIATE)
        link = self.link(job, self.service.graph.for_memory(ids[11]), reason_key="north", confidence=.8)
        link["reason"] = "北极旅行的关联理由"
        self.service.complete_association(job["job"]["id"], [link], .3)
        self.drain_associations([.3])
        hits = self.service.search("北极", ["owner"])
        self.assertEqual({h["id"] for h in hits}, set(ids[10:]))


if __name__ == "__main__":
    unittest.main()
