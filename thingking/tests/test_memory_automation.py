"""End-to-end compact evidence, cache invalidation, and unattended failure recovery."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import Clock
from memory.association_plan import SYSTEM
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.worker import MemoryWorker
from test_memory_worker_pool import DirectClient


class AutomationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.tmp.name), state_updates=False,
                           association_backfill_batch=1, job_max_attempts=1,
                           job_recovery_seconds=3600), self.clock)
        self.client = DirectClient(self.service)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def node(self, text, scope="owner"):
        mid = self.repo.add_memory("episode", scope, text)
        return self.service.graph.register_memory(mid)

    def pair(self):
        self.node("周六归还蓝色围巾")
        self.node("蓝色围巾归还改成周日")
        return self.service.claim_job(["associate"])

    def decisions(self, claim, linked=False):
        compact = claim["materials"]["compact"]
        return [{"target": card["id"], "links": [{
            "reason_key": "scarf-return", "reason": "蓝色围巾归还日期变化",
            "relation_type": "contradicts", "confidence": .8,
            "evidence_ids": [compact["source"]["evidence"][0]["id"], card["evidence"][0]["id"]],
        }] if linked else []} for card in compact["candidates"]]

    def test_worker_ids_expand_to_real_scoped_evidence(self):
        claim = self.pair()
        prompts = []
        def model(messages):
            prompts.append(messages)
            return json.dumps({"prior": .6, "decisions": self.decisions(claim, True)}, ensure_ascii=False)
        MemoryWorker(self.client, model, lambda: "")._associate(claim["job"], claim["materials"])
        self.assertEqual(self.repo.get_job(claim["job"]["id"])["status"], "done")
        edge = dict(self.repo.conn.execute("SELECT * FROM association_edges").fetchone())
        for evidence in json.loads(edge["evidence"]):
            node = self.service.graph.get(evidence["node_id"], "owner")
            self.assertIn(evidence["quote"], self.service.graph.material(node)["text"])
        self.assertEqual(len(prompts), 1)
        self.assertNotIn(claim["materials"]["source"]["id"], prompts[0][1]["content"])

    def test_missing_decision_is_not_cached_as_negative(self):
        claim = self.pair()
        with self.assertRaises(ValueError):
            self.service.complete_association(claim["job"]["id"], [], decisions=[])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_decisions").fetchone()[0], 0)
        self.assertEqual(self.repo.get_job(claim["job"]["id"])["status"], "running")

    def test_uncertain_relation_expands_once_then_settles_without_negative_cache(self):
        claim = self.pair()
        def uncertain(item):
            return [{**decision, "uncertain": True} for decision in self.decisions(item)]
        result = self.service.complete_association(claim["job"]["id"], [], decisions=uncertain(claim))
        self.assertEqual(result["status"], "expanding_evidence")
        second = self.service.claim_job(["associate"])
        self.assertEqual(second["job"]["id"], claim["job"]["id"])
        self.assertTrue(second["job"]["payload"]["evidence_expanded"])
        result = self.service.complete_association(second["job"]["id"], [], decisions=uncertain(second))
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["unresolved_targets"])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_decisions").fetchone()[0], 0)
        self.assertEqual(self.service.maintenance.stats()["exhausted"], 0)
        job = next(j for j in self.service.list_jobs()["items"] if j["id"] == second["job"]["id"])
        self.assertGreater(job["unresolved_count"], 0)

    def test_uncertain_candidate_does_not_discard_valid_relation_to_another(self):
        self.node("蓝色围巾第三条记录")
        first = self.pair()
        decisions = self.decisions(first, True)
        decisions[-1] = {"target": decisions[-1]["target"], "links": [], "uncertain": True}
        self.service.complete_association(first["job"]["id"], [], decisions=decisions)
        second = self.service.claim_job(["associate"])
        decisions = self.decisions(second, True)
        decisions[-1] = {"target": decisions[-1]["target"], "links": [], "uncertain": True}
        result = self.service.complete_association(second["job"]["id"], [], decisions=decisions)
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_decisions").fetchone()[0], 1)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 1)

    def test_unknown_or_third_party_evidence_ids_rejected(self):
        self.node("蓝色围巾另一事件")
        claim = self.pair()
        decisions = self.decisions(claim, True)
        decisions[0]["links"][0]["evidence_ids"][1] = "forged"
        with self.assertRaises(ValueError):
            self.service.complete_association(claim["job"]["id"], [], decisions=decisions)
        decisions = self.decisions(claim, True)
        decisions[0]["links"][0]["evidence_ids"][1] = claim["materials"]["compact"]["candidates"][1]["evidence"][0]["id"]
        with self.assertRaises(ValueError):
            self.service.complete_association(claim["job"]["id"], [], decisions=decisions)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 0)

    def replay(self, claim):
        node = self.service.graph.get(claim["job"]["payload"]["node_id"])
        jid = self.repo.enqueue_job("associate", "owner", {"node_id": node["id"], "revision": node["revision"]},
                                   "manual-reassessment")
        self.repo.conn.execute("UPDATE jobs SET status='done' WHERE status='pending' AND id!=?", (jid,))
        return self.service.claim_job(["associate"])

    def test_negative_cache_avoids_model_and_expires_on_source_change(self):
        claim = self.pair()
        self.service.complete_association(claim["job"]["id"], [], decisions=self.decisions(claim))
        repeat = self.replay(claim)
        self.assertEqual(repeat["materials"]["compact"]["candidates"], [])
        def forbidden(messages):
            raise AssertionError("缓存命中不应请求模型")
        MemoryWorker(self.client, forbidden, lambda: "")._associate(repeat["job"], repeat["materials"])
        self.assertEqual(self.repo.get_job(repeat["job"]["id"])["status"], "done")
        candidate = claim["materials"]["candidates"][0]
        node = self.service.graph.get(candidate["id"])
        self.service.update_memory(node["memory_id"], content="蓝色围巾改为下周一归还")
        self.repo.conn.execute("UPDATE jobs SET status='pending' WHERE id=?", (repeat["job"]["id"],))
        fresh = self.service.claim_job(["associate"])
        self.assertTrue(fresh["materials"]["compact"]["candidates"])

    def test_correction_waterline_rejects_old_decision(self):
        claim = self.pair()
        source = self.service.graph.get(claim["job"]["payload"]["node_id"])
        other = self.node("历史居住地北京")
        self.service.correct_memory(scope="owner", node_id=other["id"], operation_id="city",
            expected_digest=other["digest"], kind="fact", quote="北京", replacement="上海", explanation="纠正")
        result = self.service.complete_association(claim["job"]["id"], [], decisions=self.decisions(claim))
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_decisions").fetchone()[0], 0)
        self.assertEqual(source["scope"], "owner")

    def test_compact_input_is_bounded_and_other_scope_never_enters_plan(self):
        for i in range(15):
            self.node(f"蓝色围巾事件{i}。" + "无关描述和解释。" * 300)
        private = self.node("蓝色围巾保密约定", "qq:other:private:42")
        claim = self.service.claim_job(["associate"])
        compact = claim["materials"]["compact"]
        self.assertLessEqual(len(compact["candidates"]), 6)
        prompt = SYSTEM + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(prompt), 5000)
        self.assertNotIn(private["id"], json.dumps(claim))
        for eid, evidence in claim["job"]["payload"]["association_plan"]["catalog"].items():
            node = self.service.graph.get(evidence["node_id"], "owner")
            self.assertIn(evidence["quote"], self.service.graph.material(node)["text"], eid)

    def test_failed_job_recovers_without_resetting_lease_or_looping_forever(self):
        self.node("只有一条原始记忆")
        first = self.service.claim_job(["associate"])["job"]
        job = first
        for cycle in range(3):
            self.assertEqual(self.service.fail_job(job["id"], "网络超时",
                             expected_attempt=job["attempts"])["status"], "failed")
            self.clock.advance(3601)
            claim = self.service.claim_job(["associate"])
            if cycle < 2:
                job = claim["job"]
                self.assertGreater(job["attempts"], first["attempts"])
                self.assertEqual(job["payload"]["recovery_cycle"], cycle + 1)
                self.assertEqual(self.service.complete_association(job["id"], [],
                    expected_attempt=first["attempts"])["status"], "ignored")
            else:
                self.assertIsNone(claim)
        self.assertEqual(self.service.maintenance.stats()["exhausted"], 1)
        self.assertEqual(self.service.maintenance.stats()["recovered"], 2)
        self.assertEqual(self.repo.count_memories(), 1)

    def test_one_recovery_probe_per_kind_cooldown_and_deleted_source_settles(self):
        a, b = self.node("围巾甲"), self.node("围巾乙")
        for _ in range(2):
            job = self.service.claim_job(["associate"])["job"]
            self.service.fail_job(job["id"], "超时", expected_attempt=job["attempts"])
        self.service.delete_memory(a["memory_id"])
        self.clock.advance(3601)
        self.service.claim_job(["associate"])
        self.assertEqual(self.service.maintenance.stats()["recovered"], 1)
        self.assertEqual(self.repo.get_memory(b["memory_id"])["deleted"], 0)


if __name__ == "__main__":
    unittest.main()
