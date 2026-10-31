"""Correction, canonical event and context learning contracts, on isolated data."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import Clock, local_env
from memory.associations import digest
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings


class MemoryLearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.tmp.name), state_updates=False, retention_enabled=True), self.clock)
        self.graph = self.service.graph

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def memory(self, content, scope="owner", **meta):
        mid = self.repo.add_memory("episode", scope, content, meta=meta)
        return self.graph.register_memory(mid)

    def raw(self, content):
        result = self.service.record_turn(local_env(self.clock()), content, "收到")
        return self.graph.for_message(result["message_ids"][0])

    def link(self, a, b, reason="围巾赠送约定", key="gift"):
        cid = "c:" + digest(["owner", key])[:32]
        self.repo.conn.execute("INSERT OR IGNORE INTO association_chains VALUES(?,?,?,?,1)",
                              (cid, "owner", key, reason))
        for n in (a, b):
            self.repo.conn.execute("INSERT OR IGNORE INTO chain_memberships VALUES(?,?,.5,.5,0)", (cid, n["id"]))
        self.repo.conn.execute("INSERT OR REPLACE INTO association_edges VALUES(?,?,?,?,?,?,?,?,?,?)",
            (digest([a["id"], b["id"], key]), cid, a["id"], b["id"], "support", .8,
             "[]", a["revision"], b["revision"], "bidirectional"))
        return cid

    def correction(self, node, **extra):
        return dict(scope="owner", node_id=node["id"], operation_id="fix-1", expected_digest=node["digest"],
                    kind="fact", quote="北京", replacement="上海", explanation="用户纠正居住城市", **extra)

    def test_fact_ledger_preserves_original_message_and_is_idempotent(self):
        node = self.raw("我住在北京")
        request = self.correction(node)
        result = self.service.correct_memory(**request)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.repo.get_message(node["message_ids"][0])["content"], "我住在北京")
        current = self.graph.get(node["id"])
        self.assertNotEqual(current["digest"], node["digest"])
        self.assertIn("上海", self.graph.material(current)["text"])
        self.assertEqual(self.service.correct_memory(**request)["status"], "duplicate")
        request["replacement"] = "深圳"
        with self.assertRaises(ValueError):
            self.service.correct_memory(**request)
        self.assertEqual(len(self.service.corrections.history(node["id"], "owner")), 1)

    def test_fact_scope_and_stale_source_are_rejected_atomically(self):
        node = self.memory("住在北京")
        request = self.correction(node)
        request["scope"] = "qq:account:group:123"
        with self.assertRaises(ValueError):
            self.service.correct_memory(**request)
        self.service.update_memory(node["memory_id"], content="住在广州")
        with self.assertRaises(ValueError):
            self.service.correct_memory(**self.correction(node))
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_corrections").fetchone()[0], 0)

    def test_dependent_summary_hidden_until_repair_and_original_archived(self):
        node = self.raw("我住在北京")
        derived = self.memory("管理员住在北京", source_message_ids=node["message_ids"])
        self.service.correct_memory(**self.correction(node))
        self.assertFalse(self.graph.valid(derived))
        self.assertNotIn(derived["memory_id"], [r["id"] for r in self.service.search("北京", ["owner"])])
        self.assertIsNotNone(self.service.memory_detail(node_id=derived["id"]))
        claimed = self.service.claim_job(["repair_memory"])
        self.assertIn("上海", json.dumps(claimed["materials"], ensure_ascii=False))
        result = self.service.complete_memory_repair(claimed["job"]["id"], "管理员住在上海")
        self.assertEqual(result["status"], "done")
        memory = self.repo.get_memory(derived["memory_id"])
        self.assertEqual(memory["content"], "管理员住在上海")
        self.assertEqual(memory["meta"]["repair_history"][0]["content"], "管理员住在北京")
        self.assertTrue(self.graph.valid(self.graph.for_memory(derived["memory_id"])))

    def test_paraphrased_dependencies_repair_transitively_without_touching_unrelated_sources(self):
        source = self.raw("我住在北京")
        summary = self.memory("管理员居住在首都", source_message_ids=source["message_ids"])
        diary = self.memory("朋友来自首都", source_memory_ids=[summary["memory_id"]])
        unrelated = self.memory("同事也住在首都")
        self.service.correct_memory(**self.correction(source))
        for item in (summary, diary):
            self.assertEqual(self.repo.get_memory(item["memory_id"])["meta"]["repair_pending"], "fix-1")
            self.assertFalse(self.graph.valid(item))
        self.assertTrue(self.graph.valid(unrelated))
        claimed = self.service.claim_job(["repair_memory"])
        self.assertEqual(claimed["job"]["payload"]["memory_id"], summary["memory_id"])
        self.assertEqual(claimed["materials"]["corrections"][0]["replacement"], "上海")
        self.service.complete_memory_repair(claimed["job"]["id"], "管理员住在上海")
        next_job = self.service.claim_job(["repair_memory"])
        self.assertEqual(next_job["job"]["payload"]["memory_id"], diary["memory_id"])
        self.assertIn("上海", json.dumps(next_job["materials"], ensure_ascii=False))
        self.service.complete_memory_repair(next_job["job"]["id"], "朋友住在上海")
        self.assertTrue(self.graph.valid(self.graph.for_memory(diary["memory_id"])))
        self.assertEqual(self.repo.get_message(source["message_ids"][0])["content"], "我住在北京")

    def test_repair_cannot_commit_after_new_source_revision(self):
        source = self.memory("北京是居住城市")
        child = self.memory("住在北京", source_memory_ids=[source["memory_id"]])
        self.service.correct_memory(**self.correction(source))
        job = self.service.claim_job(["repair_memory"])
        self.service.update_memory(source["memory_id"], content="住在深圳")
        self.assertEqual(self.service.complete_memory_repair(job["job"]["id"], "住在上海")["status"], "stale")
        self.assertTrue(self.repo.get_memory(child["memory_id"])["meta"].get("repair_pending"))

    def test_reason_rejection_quarantines_old_edge_and_stale_result(self):
        a, b = self.memory("围巾甲"), self.memory("围巾乙")
        cid = self.link(a, b)
        claimed = self.service.claim_job(["associate"])
        revision = self.repo.conn.execute("SELECT revision FROM association_chains WHERE id=?", (cid,)).fetchone()[0]
        request = dict(scope="owner", node_id=a["id"], operation_id="reject-gift", expected_digest=a["digest"],
                       kind="reject_reason", chain_id=cid, expected_chain_revision=revision,
                       explanation="两次记录不代表赠送约定")
        self.service.correct_memory(**request)
        self.assertEqual(self.repo.conn.execute("SELECT confidence FROM association_edges").fetchone()[0], 0)
        self.assertEqual(self.service.complete_association(claimed["job"]["id"], [], .5)["status"], "stale")
        self.assertEqual(self.service.search("赠送约定", ["owner"]), [])
        self.assertEqual(self.service.correct_memory(**request)["status"], "duplicate")
        fresh = self.service.claim_job(["associate"])
        source = fresh["materials"]["source"]
        target = next(c for c in fresh["materials"]["candidates"] if c["id"] != source["id"])
        link = {"target_id": target["id"], "reason_key": "gift", "reason": "围巾赠送约定",
                "relation_type": "support", "confidence": .9,
                "evidence": [{"node_id": source["id"], "quote": source["content"]},
                             {"node_id": target["id"], "quote": target["content"]}]}
        self.assertEqual(self.service.complete_association(fresh["job"]["id"], [link])["status"], "done")
        self.assertEqual(self.repo.conn.execute("SELECT MAX(confidence) FROM association_edges").fetchone()[0], 0)

    def test_same_round_receipts_and_summary_share_event_but_next_round_does_not(self):
        prefix = "experience:" + "a" * 64
        nodes = []
        for turn, suffix in (("round1", "operation"), ("round1", "turn"), ("round2", "operation")):
            ref = f"{prefix}:{turn}:{suffix}"
            self.service.record_experience(local_env(self.clock()), ref, {
                "turn_id": turn, "category": "observation", "action": "website_snapshot",
                "status": "succeeded", "result": "围巾观察", "source_message_refs": []})
            mid = self.repo.conn.execute("SELECT memory_id FROM experiences WHERE source_ref=?", (ref,)).fetchone()[0]
            nodes.append(self.graph.for_memory(mid))
        summary = self.memory("围巾摘要", source_memory_ids=[nodes[0]["memory_id"]])
        self.assertFalse(self.graph.independent(nodes[0], nodes[1]))
        self.assertFalse(self.graph.independent(nodes[1], summary))
        self.assertTrue(self.graph.independent(nodes[0], nodes[2]))
        self.assertNotEqual(nodes[0]["family"], nodes[1]["family"])
        cid = self.link(nodes[0], nodes[1])
        self.assertAlmostEqual(self.graph.weights(cid, "owner")[nodes[0]["id"]]["recall_strength"], nodes[0]["prior"])
        self.graph.quarantine_same_events()
        self.assertEqual(self.repo.conn.execute("SELECT confidence FROM association_edges").fetchone()[0], 0)
        self.assertEqual(len(self.service.provenance.siblings(nodes[0], self.graph)), 2)

    def test_missing_namespace_never_merges_by_topic_turn_or_url(self):
        for ref in ("receipt-a", "receipt-b"):
            self.service.record_experience(local_env(self.clock()), ref, {
                "turn_id": "same", "status": "succeeded", "action": "website_snapshot",
                "result": "https://example.org 相同页面"})
        ids = [r[0] for r in self.repo.conn.execute("SELECT memory_id FROM experiences")]
        a, b = [self.graph.for_memory(mid) for mid in ids]
        self.assertTrue(self.graph.independent(a, b))

    def test_same_event_presentations_and_citations_count_once(self):
        nodes = []
        for suffix in ("receipt", "continued"):
            ref = "experience:" + "b" * 64 + ":round:" + suffix
            self.service.record_experience(local_env(self.clock()), ref, {
                "turn_id": "round", "status": "succeeded", "action": "website_snapshot", "result": "围巾"})
            mid = self.repo.conn.execute("SELECT memory_id FROM experiences WHERE source_ref=?", (ref,)).fetchone()[0]
            nodes.append(self.graph.for_memory(mid))
        shown = [{"node_id": n["id"], "node_digest": n["digest"], "spans": ["围巾"]} for n in nodes]
        citations = [{"source_id": n["id"], "answer_span": "围巾", "source_quote": "围巾"} for n in nodes]
        self.assertEqual(self.service.usage.record("owner", "answered", shown, citations, "围巾"),
                         {"presented": 1, "cited": 1})

    def test_topk_reserves_one_place_per_event_using_strongest_representation(self):
        a = self.memory("围巾原始记录")
        b = self.memory("围巾摘要", source_memory_ids=[a["memory_id"]])
        c = self.memory("独立围巾记录")
        self.repo.conn.execute("UPDATE association_nodes SET ready=1,prior=.9 WHERE id=?", (b["id"],))
        self.repo.conn.execute("UPDATE association_nodes SET ready=1 WHERE id IN (?,?)", (a["id"], c["id"]))
        self.repo.enqueue_job("diary", "owner", {}, "test-freeze")
        job = self.repo.claim_job(300, kinds=["diary"])
        nodes = [self.graph.get(n["id"]) for n in (a, b, c)]
        snapshot = self.service.retention.freeze(job, [self.repo.get_memory(n["memory_id"]) for n in nodes], nodes, 2)
        self.assertEqual(snapshot["data"]["protected"], [b["id"], c["id"]])

    def context(self):
        a, b = self.raw("蓝色围巾"), self.raw("红色围巾")
        gift, hobby = self.link(a, b), self.link(a, b, reason="围巾收藏爱好", key="hobby")
        row = {"node_id": a["id"], "node_digest": a["digest"], "scope": "owner", "chain_ids": [gift, hobby]}
        self.service.competition.mint("生日礼物", [row])
        presentation = {**row, "spans": ["蓝色围巾"]}
        citation = {"source_id": a["id"], "chain_id": gift, "source_quote": "蓝色围巾", "answer_span": "礼物"}
        return a, gift, hobby, presentation, citation

    def test_real_citation_shifts_only_same_question_and_does_not_learn_twice(self):
        a, gift, hobby, shown, citation = self.context()
        self.service.usage.record("owner", "turn1", [shown], [], "")
        self.assertEqual(self.service.competition.shares("owner", "生日礼物", [gift, hobby])[gift], .5)
        self.service.usage.record("owner", "turn1", [shown], [citation, citation], "礼物")
        shares = self.service.competition.shares("owner", "生日礼物", [gift, hobby])
        self.assertAlmostEqual(shares[gift], 2 / 3)
        self.assertAlmostEqual(sum(shares.values()), 1)
        self.service.usage.record("owner", "turn1", [shown], [citation], "礼物")
        self.assertEqual(self.service.competition.shares("owner", "生日礼物", [gift, hobby]), shares)
        self.assertEqual(self.service.competition.shares("owner", "旅行计划", [gift, hobby])[gift], .5)
        self.assertEqual(self.service.competition.shares("group", "生日礼物", [gift, hobby])[gift], .5)

    def test_forged_context_or_unquoted_citation_cannot_learn(self):
        a, gift, hobby, shown, citation = self.context()
        self.service.usage.record("owner", "bad-context", [{**shown, "context_id": "ctx:forged"}], [citation], "礼物")
        self.service.usage.record("owner", "no-evidence", [shown], [{**citation, "source_quote": ""}], "礼物")
        self.assertEqual(self.service.competition.shares("owner", "生日礼物", [gift, hobby])[gift], .5)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM context_credit_events").fetchone()[0], 0)

    def test_reason_correction_invalidates_previous_context_snapshot(self):
        a, gift, hobby, shown, citation = self.context()
        revision = self.repo.conn.execute("SELECT revision FROM association_chains WHERE id=?", (gift,)).fetchone()[0]
        self.service.correct_memory(scope="owner", node_id=a["id"], operation_id="fix-reason",
            expected_digest=a["digest"], kind="reason", chain_id=gift, expected_chain_revision=revision,
            replacement="围巾的购买记录", explanation="这份资料不是赠送约定")
        self.service.usage.record("owner", "late-citation", [shown], [citation], "礼物")
        self.assertEqual(self.service.competition.shares("owner", "生日礼物", [gift, hobby])[gift], .5)

    def test_temporal_fact_reads_use_corrected_value_and_keep_original_version(self):
        node = self.memory("管理员住在北京")
        self.repo.temporal.put_fact(node["memory_id"], "owner", "owner", "city", "北京", self.clock() - 10)
        self.service.correct_memory(**self.correction(node))
        self.assertEqual(self.service.facts("owner")[0]["value"], "上海")
        self.assertEqual(json.loads(self.repo.conn.execute("SELECT value FROM fact_versions").fetchone()[0]), "北京")

    def test_corrected_reason_does_not_inherit_old_reason_credit(self):
        a, gift, hobby, shown, citation = self.context()
        self.service.usage.record("owner", "used-old-reason", [shown], [citation], "礼物")
        self.assertAlmostEqual(self.service.competition.shares("owner", "生日礼物", [gift, hobby])[gift], 2 / 3)
        revision = self.repo.conn.execute("SELECT revision FROM association_chains WHERE id=?", (gift,)).fetchone()[0]
        self.service.correct_memory(scope="owner", node_id=a["id"], operation_id="reason-reset",
            expected_digest=a["digest"], kind="reason", chain_id=gift, expected_chain_revision=revision,
            replacement="围巾的购买记录", explanation="原有赠送理由有误")
        self.assertEqual(self.service.competition.shares("owner", "生日礼物", [gift, hobby])[gift], .5)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM context_credit_events").fetchone()[0], 1)

    def test_numeric_fact_correction_suppresses_unsafe_old_scalar(self):
        node = self.memory("年龄24")
        self.repo.temporal.put_fact(node["memory_id"], "owner", "owner", "age", 24, self.clock() - 10)
        self.service.correct_memory(scope="owner", node_id=node["id"], operation_id="age-fix",
            expected_digest=node["digest"], kind="fact", quote="24", replacement="25", explanation="用户纠正年龄")
        self.assertEqual(self.service.facts("owner"), [])
        self.assertEqual(json.loads(self.repo.conn.execute("SELECT value FROM fact_versions").fetchone()[0]), 24)

    def test_late_event_identity_invalidates_already_scored_edges(self):
        first = "experience:" + "e" * 64 + ":late:receipt"
        second = "experience:" + "e" * 64 + ":late:continued"
        for ref in (first, second):
            self.service.record_experience(local_env(self.clock()), ref,
                {"action": "website_snapshot", "status": "succeeded", "result": "围巾观察"})
        ids = [r[0] for r in self.repo.conn.execute("SELECT memory_id FROM experiences")]
        a, b = [self.graph.for_memory(mid) for mid in ids]
        cid = self.link(a, b)
        self.graph.weights(cid, "owner")
        self.service.provenance.register("owner", first, {"turn_id": "late"})
        self.service.provenance.register("owner", second, {"turn_id": "late"})
        self.assertFalse(self.graph.independent(a, b))
        self.assertEqual(self.repo.conn.execute("SELECT confidence FROM association_edges").fetchone()[0], 0)
        self.assertAlmostEqual(self.graph.weights(cid, "owner")[a["id"]]["recall_strength"], a["prior"])

    def test_staged_runtime_correction_requires_owner_user_evidence(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        from tools.context import ToolContext, tool_context
        from tools.memory_tool import correct_memory
        node = self.raw("管理员住在北京")
        outbox = MemoryOutbox(Path(self.tmp.name) / "correction-outbox.db")
        journal = MemoryJournal(outbox, local_env(self.clock()), "correction-turn")
        journal.source_message_refs = [journal.prefix + ":user", journal.prefix + ":assistant"]
        journal.turn_message_refs = list(journal.source_message_refs)
        outbox.record_message(journal.envelope, "user", "我住在上海", journal.source_message_refs[0])
        outbox.record_message(journal.envelope, "assistant", "已经纠正", journal.source_message_refs[1])
        with tool_context(ToolContext(journal=journal, user_text="我住在上海")):
            journal.present_memory({"node_id": node["id"], "node_digest": node["digest"]}, ["北京"])
            journal.confirm_memory_presentations([{"content": node["id"] + " 北京"}])
            with self.assertRaises(ValueError):
                correct_memory(node["id"], "北京", "深圳", "纠正", "我住在上海")
            correct_memory(node["id"], "北京", "上海", "用户纠正", "我住在上海")
            journal.turn_result = "已经纠正"
        outbox.sync(lambda method, **params: getattr(self.service, method)(**params))
        self.assertIn("上海", self.graph.material(self.graph.get(node["id"]))["text"])
        outbox.sync(lambda method, **params: getattr(self.service, method)(**params))
        self.assertEqual(len(self.service.corrections.history(node["id"], "owner")), 1)

    def test_group_owner_and_autonomous_round_cannot_stage_correction(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        from chatBot.messages import Identity
        from tools.context import ToolContext, tool_context
        node = self.raw("管理员住在北京")
        contexts = [
            ToolContext(identity=Identity("qq", "owner", group="group", owner=True), user_text="上海"),
            ToolContext(autonomous=True, user_text="上海"),
            ToolContext(identity=Identity("qq", "stranger"), user_text="上海"),
        ]
        for i, context in enumerate(contexts):
            journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / f"blocked-{i}.db"), local_env(), f"blocked-{i}")
            journal.present_memory({"node_id": node["id"], "node_digest": node["digest"]}, ["北京"])
            journal.confirm_memory_presentations([{"content": node["id"] + " 北京"}])
            from dataclasses import replace
            with tool_context(replace(context, journal=journal)):
                with self.assertRaises(PermissionError):
                    journal.correct_memory(node["id"], "北京", "上海", "纠正", "上海")


if __name__ == "__main__":
    unittest.main()
