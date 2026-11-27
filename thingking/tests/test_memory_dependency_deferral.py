"""Dependency waits stay pending without churning leases or model failures."""
from helpers import approved_diary
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]


class MemoryDependencyDeferralTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bines-dependency-wait-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(root),
            "BINES_GATEWAY_DIR": str(root / "gateway"), "BINES_HEALTH_DIR": str(root / "health"),
            "BINES_MODEL_USAGE_PATH": str(root / "usage.db"),
            "BINES_WEBSITE_BRIDGE_SPOOL": str(root / "website"),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        from memory.lexical_index import LexicalIndex
        from memory.repository import MemoryRepository
        from memory.service import MemoryService
        from memory.settings import MemorySettings
        self.now = 1_792_800_000.0
        self.repo = MemoryRepository(root / "memory.db", clock=lambda: self.now)
        self.addCleanup(self.repo.close)
        settings = MemorySettings(data_dir=root, state_updates=False, retention_enabled=True,
                                  associations_enabled=True, summarize_trigger=1000)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), settings, clock=lambda: self.now)
        self.service._last_diary_check = self.now
        self.service._last_index_check = self.now

    def diary(self, **extra):
        timestamp = self.now - 86400
        day = self.service._day(timestamp)
        mid = self.repo.add_memory("episode", "owner", "约定周五归还围巾", day_key=day,
                                   start_ts=timestamp, end_ts=timestamp)
        node = self.service.graph.register_memory(mid)
        jid = self.repo.enqueue_job("diary", "owner", {"day_key": day, **extra}, "diary-waits")
        return jid, node

    def finish_associations(self):
        while claim := self.service.claim_job(["associate"]):
            self.service.complete_association(claim["job"]["id"], [], expected_attempt=claim["job"]["attempts"])

    def test_diary_dependency_wait_does_not_churn_claims_or_consume_failure_budget(self):
        jid, node = self.diary(generation_failures=2, recovery_cycle=1)
        self.repo.conn.execute("UPDATE jobs SET last_error='earlier model failure' WHERE id=?", (jid,))
        started = self.now
        for offset in range(0, 30, 2):
            self.now = started + offset
            self.assertIsNone(self.service.claim_job(["diary"]))
        job = self.repo.get_job(jid)
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["available_at"], started + 30)
        self.assertEqual(job["updated_at"], started)
        self.assertIsNone(job["lease_until"])
        self.assertIsNone(job["started_at"])
        self.assertEqual(job["lease_recoveries"], 0)
        self.assertEqual(job["payload"]["generation_failures"], 2)
        self.assertEqual(job["payload"]["recovery_cycle"], 1)
        self.assertEqual(job["last_error"], "earlier model failure")
        self.assertIsNone(self.service.retention.get(jid))
        self.assertFalse(self.service.graph.get(node["id"])["ready"])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_model_calls").fetchone()[0], 0)
        self.now = started + 30
        self.assertIsNone(self.service.claim_job(["diary"]))
        self.assertEqual(self.repo.get_job(jid)["attempts"], 2)
        self.assertEqual(self.repo.get_job(jid)["available_at"], started + 60)

    def test_diary_runs_after_dependencies_finish_and_short_delay_expires(self):
        jid, node = self.diary()
        started = self.now
        self.assertIsNone(self.service.claim_job(["diary"]))
        self.finish_associations()
        self.assertTrue(self.service.graph.get(node["id"])["ready"])
        self.assertIsNone(self.service.claim_job(["diary"]))
        self.now = started + 30
        claim = self.service.claim_job(["diary"])
        self.assertEqual(claim["job"]["id"], jid)
        self.assertEqual(claim["job"]["attempts"], 2)
        self.assertEqual(claim["job"]["started_at"], self.now)
        self.assertIn(node["id"], claim["materials"]["retention"]["data"]["node_ids"])
        result = self.service.complete_diary(jid, "周五归还围巾的约定已记录",
                                             expected_attempt=claim["job"]["attempts"], attribution_review=approved_diary(claim, '周五归还围巾的约定已记录'))
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.repo.get_job(jid)["status"], "done")

    def test_summary_waits_for_raw_dependency_without_skipping_retention(self):
        from memory.channels import local_envelope
        past = self.now - 86400
        result = self.service.record_turn(local_envelope("", timestamp=past).to_dict(), "围巾周五归还", "记住了")
        mids = result["message_ids"]
        node = self.service.graph.for_message(mids[0])
        jid = self.repo.enqueue_job("summarize", "owner", {"source_message_ids": mids},
                                    "summary-waits", conversation_id="local")
        started = self.now
        self.assertIsNone(self.service.claim_job(["summarize"]))
        for _ in range(10):
            self.assertIsNone(self.service.claim_job(["summarize"]))
        self.assertEqual(self.repo.get_job(jid)["attempts"], 1)
        self.assertIsNone(self.service.retention.get(jid))
        self.finish_associations()
        self.now = started + 30
        claim = self.service.claim_job(["summarize"])
        self.assertEqual(claim["job"]["id"], jid)
        self.assertIn(node["id"], claim["materials"]["retention"]["data"]["node_ids"])
        self.assertEqual(claim["materials"]["source_message_ids"], mids)


if __name__ == "__main__":
    unittest.main()
