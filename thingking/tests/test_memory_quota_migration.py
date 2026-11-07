"""Upgrade quota-blocked jobs without replaying completed work or stale claims."""
import tempfile
import unittest
from pathlib import Path

from helpers import Clock
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings


DAILY_ERROR = "background daily budget deferred"
CALL_ERROR = "model call budget exhausted"


class QuotaMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.path = Path(self.tmp.name) / "memory.db"
        self.repo = MemoryRepository(self.path, self.clock)
        self.addCleanup(lambda: self.repo.close())
        self.settings = MemorySettings(data_dir=Path(self.tmp.name), state_updates=False,
                                       associations_enabled=False)

    def service(self):
        return MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)

    def job(self, status, error, payload=None):
        jid = self.repo.enqueue_job("associate", "owner", payload or {}, f"job:{self.repo.job_counts()}")
        self.repo.conn.execute("""UPDATE jobs SET status=?,last_error=?,attempts=7,
            lease_recoveries=2,available_at=?,lease_until=? WHERE id=?""",
            (status, error, self.clock() + 86400, self.clock() + 420 if status == "running" else None, jid))
        return jid

    def test_startup_resumes_only_quota_blocked_jobs_and_is_idempotent(self):
        progress = {"association_completed": {"target": "digest"}, "generation_failures": 2,
                    "recovery_cycle": 1}
        daily = self.job("pending", DAILY_ERROR, progress)
        capped = self.job("failed", CALL_ERROR, {**progress, "terminal_reason": "model_call_budget"})
        untouched = []
        for status, error, payload in (
            ("running", DAILY_ERROR, {}),
            ("done", DAILY_ERROR, {}),
            ("cancelled", DAILY_ERROR, {}),
            ("pending", "TimeoutError: provider unavailable", {}),
            ("failed", "ValueError: invalid JSON", {}),
            ("failed", "lease recovery budget exhausted", {"terminal_reason": "lease_recovery_budget"}),
            ("pending", DAILY_ERROR, {"terminal_reason": "lease_recovery_budget"}),
            ("failed", CALL_ERROR, {}),
        ):
            jid = self.job(status, error, payload)
            untouched.append(self.repo.get_job(jid))

        self.service()
        for jid in (daily, capped):
            with self.subTest(job=jid):
                job = self.repo.get_job(jid)
                self.assertEqual(job["status"], "pending")
                self.assertLessEqual(job["available_at"], self.clock())
                self.assertIsNone(job["lease_until"])
                self.assertIsNone(job["last_error"])
                self.assertEqual(job["payload"], progress)
                self.assertEqual((job["attempts"], job["lease_recoveries"]), (7, 2))
        for before in untouched:
            self.assertEqual(self.repo.get_job(before["id"]), before)

        snapshots = [self.repo.get_job(jid) for jid in (daily, capped)]
        self.repo.close()
        self.clock.advance(10)
        self.repo = MemoryRepository(self.path, self.clock)
        self.service()
        self.assertEqual([self.repo.get_job(jid) for jid in (daily, capped)], snapshots)

    def test_resumed_claim_rejects_old_worker_start_and_completion(self):
        jid = self.job("pending", DAILY_ERROR)
        service = self.service()
        claimed = self.repo.claim_job(300)
        self.assertIsNotNone(claimed)
        self.assertEqual((claimed["id"], claimed["attempts"]), (jid, 8))
        event = {"call_id": "a" * 32, "status": "started", "started_at": self.clock()}
        with self.assertRaisesRegex(ValueError, "租约"):
            service.record_model_call(jid, event, expected_attempt=7)
        self.assertEqual(service.fail_job(jid, "late failure", expected_attempt=7)["status"], "ignored")
        self.assertEqual(service.complete_association(jid, [], expected_attempt=7)["status"], "ignored")
        self.assertEqual(service.record_model_call(jid, event, expected_attempt=8)["status"], "recorded")
        self.assertEqual(self.repo.get_job(jid)["status"], "running")


if __name__ == "__main__":
    unittest.main()
