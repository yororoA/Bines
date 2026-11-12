"""Long diary evidence, automatic expansion and scoped idempotent rechecks."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import Clock
from memory.association_plan import POLICY
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.worker import MemoryWorker


class PairEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(1794427399)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), MemorySettings(
            data_dir=Path(self.tmp.name), state_updates=False, association_backfill_batch=0), self.clock)

    def diary(self, text, day):
        return self.service.graph.register_memory(
            self.repo.add_memory("diary", "owner", text, day_key=day))

    def claim_diary(self):
        food = self.diary("午饭没吃，下午两点有课，我提醒垫点东西，后来吃了面包牛奶。", "2026-11-10")
        device = self.diary("电路设计实验课上一起查了电阻的色环和阻值。", "2026-11-09")
        self.repo.conn.execute("UPDATE jobs SET status='done'")
        source = self.diary(
            "早上聊了称呼和昵称。\n" + "看了新鲜事，也聊了几句。" * 80
            + "\n中午没吃，下午下课再吃，我提醒先垫一口。\n"
            + "电路设计陪着上课，看电阻色环算阻值。\n晚上才聊了名字。",
            "2026-11-11")
        return source, food, device, self.service.claim_job(["associate"])

    def test_each_candidate_gets_matching_source_evidence_and_event_date(self):
        source, food, device, claim = self.claim_diary()
        plan = claim["job"]["payload"]["association_plan"]
        cards = claim["materials"]["compact"]["candidates"]
        self.assertEqual({plan["aliases"][c["id"]] for c in cards}, {food["id"], device["id"]})
        expected = {food["id"]: "中午没吃", device["id"]: "电路设计"}
        for card in cards:
            quotes = [plan["catalog"][eid]["quote"] for eid in card["source_evidence_ids"]]
            self.assertTrue(any(expected[plan["aliases"][card["id"]]] in q for q in quotes), quotes)
            self.assertTrue(card["events"][0]["day_key"].startswith("2026-11-"))
        self.assertEqual(plan["prompt"]["source"]["events"][0]["day_key"], "2026-11-11")
        for evidence in plan["catalog"].values():
            node = self.service.graph.get(evidence["node_id"])
            self.assertIn(evidence["quote"], self.service.graph.material(node)["text"])
        self.assertTrue(plan["prompt"]["source"]["excerpted"])
        self.assertEqual(source["revision"], 1)

    def test_negative_partial_evidence_expands_once_before_caching(self):
        source, _, _, claim = self.claim_diary()
        def negative(item):
            return [{"target": c["id"], "links": []} for c in item["materials"]["compact"]["candidates"]]
        first = self.service.complete_association(claim["job"]["id"], [], decisions=negative(claim))
        self.assertEqual(first["status"], "expanding_evidence")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_decisions").fetchone()[0], 0)
        expanded = self.service.claim_job(["associate"])
        self.assertEqual(claim["job"]["id"], expanded["job"]["id"])
        self.assertTrue(expanded["job"]["payload"]["evidence_expanded"])
        result = self.service.complete_association(expanded["job"]["id"], [], decisions=negative(expanded))
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_decisions").fetchone()[0], 2)
        self.assertEqual(self.service.graph.get(source["id"])["revision"], 1)

    def test_recheck_is_scoped_idempotent_and_preserves_memory_and_revision(self):
        source = self.diary("完整的小事件", "2026-11-11")
        claim = self.service.claim_job(["associate"])
        self.service.complete_association(claim["job"]["id"], [], decisions=[])
        params = dict(node_id=source["id"], scope="owner", expected_digest=source["digest"],
                      operation_id="repair-pair-evidence")
        original = self.repo.get_memory(source["memory_id"])
        first = self.service.recheck_association(**params)
        self.assertEqual(first, self.service.recheck_association(**params))
        for changes in ({"scope": None}, {"scope": "qq:other"}, {"expected_digest": "stale"}):
            with self.assertRaises(ValueError):
                self.service.recheck_association(**{**params, **changes})
        retry = self.service.claim_job(["associate"])
        self.service.complete_association(retry["job"]["id"], [], decisions=[])
        self.assertEqual(self.service.recheck_association(**params), {"id": first["id"], "status": "done"})
        self.assertEqual(self.repo.get_memory(source["memory_id"]), original)
        self.assertEqual(self.service.graph.get(source["id"])["revision"], source["revision"])

    def test_previous_policy_plan_is_stale_and_cannot_submit(self):
        _, _, _, claim = self.claim_diary()
        payload = dict(claim["job"]["payload"])
        payload["association_plan"]["policy"] = "evidence-ids-v2-pages"
        self.repo.update_job_payload(claim["job"]["id"], payload)
        self.assertFalse(self.service.association_planner.current(self.repo.get_job(claim["job"]["id"])))
        with self.assertRaises(ValueError):
            self.service.complete_association(claim["job"]["id"], [], decisions=[])
        self.assertEqual(POLICY, "evidence-ids-v4-verified")

    def short_pair(self):
        target = self.diary("借出的蓝围巾约定周六归还。", "2026-11-10")
        self.repo.conn.execute("UPDATE jobs SET status='done'")
        source = self.diary("同一条蓝围巾改成周日归还。", "2026-11-11")
        claim = self.service.claim_job(["associate"])
        compact = claim["materials"]["compact"]
        card = compact["candidates"][0]
        data = {"prior": .6, "decisions": [{"target": card["id"], "links": [{
            "reason_key": "scarf-return", "reason": "同一蓝围巾归还日期变化",
            "relation_type": "event", "direction": "bidirectional", "confidence": .8,
            "evidence_ids": [compact["source"]["evidence"][0]["id"], card["evidence"][0]["id"]],
        }]}]}
        return source, target, claim, data

    def test_verifier_rejects_unfounded_proposals_before_any_write(self):
        source, _, claim, data = self.short_pair()
        calls = []
        def verify(messages):
            calls.append(messages)
            self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 0)
            return '{"verdicts":[{"target":"t1","supported":false}]}'
        worker = MemoryWorker(None, verify, lambda: "")
        verified = worker._verify_compact_association(data, claim["materials"]["compact"])
        self.assertEqual(verified["decisions"][0]["links"], [])
        self.assertTrue(data["decisions"][0]["links"])  # Never mutate the proposed data.
        self.service.complete_association(claim["job"]["id"], [], decisions=verified["decisions"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.service.memory_detail(node_id=source["id"])["edges"], [])

    def test_verifier_cannot_approve_a_missing_or_invented_target(self):
        _, _, claim, data = self.short_pair()
        for answer in ('{"verdicts":[]}', '{"verdicts":[{"target":"other","supported":true}]}',
                       '{"verdicts":[{"target":"t1","supported":1}]}'):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                MemoryWorker(None, lambda _: answer, lambda: "")._verify_compact_association(
                    data, claim["materials"]["compact"])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 0)

    def test_recheck_removes_explicitly_rejected_edges_and_orphan_memberships(self):
        source, _, claim, data = self.short_pair()
        self.service.complete_association(claim["job"]["id"], [], decisions=data["decisions"])
        self.assertEqual(len(self.service.memory_detail(node_id=source["id"])["edges"]), 1)
        self.service.recheck_association(source["id"], "owner", source["digest"], "revalidate")
        recheck = self.service.claim_job(["associate"])
        # Versioned verification policy can reuse only previously verified results.
        self.repo.conn.execute("DELETE FROM association_decisions")
        plan = self.service._job_materials(recheck["job"])["compact"]
        negatives = [{"target": c["id"], "links": []} for c in plan["candidates"]]
        self.service.complete_association(recheck["job"]["id"], [], decisions=negatives)
        self.assertEqual(self.service.memory_detail(node_id=source["id"])["edges"], [])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM chain_memberships").fetchone()[0], 0)
        self.assertEqual(self.service.graph.get(source["id"])["revision"], 1)


if __name__ == "__main__":
    unittest.main()
