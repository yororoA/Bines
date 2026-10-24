"""Bounded background work and stale claim protection on isolated data."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import Clock, local_env
from memory.client import MemoryClient
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.worker import MemoryWorker


class DirectClient(MemoryClient):
    def __init__(self, service):
        self.service = service

    def call(self, method, **params):
        return getattr(self.service, method)(**params)


class WorkerClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.tmp.name), state_updates=False, job_max_attempts=1), self.clock)
        self.client = DirectClient(self.service)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def node(self, content):
        mid = self.repo.add_memory("episode", "owner", content)
        return self.service.graph.register_memory(mid)

    def test_old_attempt_cannot_complete_or_fail_reclaimed_job(self):
        self.node("首次围巾约定")
        first = self.service.claim_job(["associate"])["job"]
        self.clock.advance(421)
        second = self.service.claim_job(["associate"])["job"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["attempts"], first["attempts"] + 1)
        self.assertEqual(self.client.complete_association(first["id"], [], expected_attempt=first["attempts"])["status"], "ignored")
        self.assertEqual(self.client.fail_job(first["id"], "旧请求超时", expected_attempt=first["attempts"])["status"], "ignored")
        self.assertEqual(self.repo.get_job(second["id"])["status"], "running")
        self.clock.advance(3)
        self.assertEqual(self.client.complete_association(second["id"], [], expected_attempt=second["attempts"])["status"], "done")
        self.assertEqual(self.repo.get_job(second["id"])["duration_seconds"], 3)

    def test_no_candidates_finishes_without_model_call(self):
        self.node("唯一的独立记录")
        def forbidden(_messages):
            raise AssertionError("无候选不得请求模型")
        self.assertTrue(MemoryWorker(self.client, forbidden, lambda: "").run_once(["associate"]))
        self.assertEqual(self.repo.job_counts(), {"done": 1})

    def test_receipt_revision_associates_only_after_summary(self):
        record = dict(turn_id="r", action="website_snapshot", status="succeeded", result="围巾观察")
        self.service.record_experience(local_env(self.clock()), "receipt", record)
        self.service.record_experience(local_env(self.clock()), "receipt", {**record, "result": "新的围巾观察"}, revision=2)
        self.assertIsNone(self.service.claim_job(["associate"]))
        summary = self.service.claim_job(["summarize_experience"])
        self.assertEqual(summary["job"]["payload"]["revision"], 2)
        self.assertEqual(self.service.complete_experience_summary(summary["job"]["id"], "新的围巾观察")["status"], "done")
        associations = self.repo.conn.execute("SELECT * FROM jobs WHERE kind='associate' AND status='pending'").fetchall()
        self.assertEqual(len(associations), 1)
        memory = self.repo.conn.execute("SELECT memory_id FROM experiences").fetchone()[0]
        self.assertFalse(self.repo.get_memory(memory)["meta"]["experience_summary_pending"])

    def test_failed_experience_summary_releases_auditable_fallback(self):
        self.service.record_experience(local_env(self.clock()), "failed-summary",
            dict(turn_id="r", action="website_snapshot", status="failed", result="浏览器未加载"))
        claimed = self.service.claim_job(["summarize_experience"])
        self.assertEqual(self.client.fail_job(claimed["job"]["id"], "模型失败",
            expected_attempt=claimed["job"]["attempts"])["status"], "failed")
        self.assertIsNotNone(self.service.claim_job(["associate"]))
        memory = self.repo.conn.execute("SELECT memory_id FROM experiences").fetchone()[0]
        self.assertTrue(self.repo.get_memory(memory)["meta"]["experience_summary_failed"])
        self.assertIn("failed", self.repo.get_memory(memory)["content"])

    def test_repair_worker_rebuilds_corrected_source(self):
        turn = self.service.record_turn(local_env(self.clock()), "住在北京", "收到")
        raw = self.service.graph.for_message(turn["message_ids"][0])
        child = self.repo.add_memory("episode", "owner", "住在北京", meta={"source_message_ids": raw["message_ids"]})
        self.service.graph.register_memory(child)
        self.service.correct_memory(scope="owner", node_id=raw["id"], operation_id="move",
            expected_digest=raw["digest"], kind="fact", quote="北京", replacement="上海", explanation="居住地纠正")
        prompts = []
        def model(messages):
            prompts.append(json.dumps(messages, ensure_ascii=False))
            return '{"content":"住在上海"}'
        MemoryWorker(self.client, model, lambda: "").run_once(["repair_memory"])
        self.assertIn("上海", prompts[0])
        self.assertEqual(self.repo.get_memory(child)["content"], "住在上海")
        self.assertNotIn("repair_pending", self.repo.get_memory(child)["meta"])

    def test_bad_quote_does_not_discard_other_verified_links_or_requeue_job(self):
        self.node("周六归还蓝色围巾")
        self.node("蓝色围巾周六归还")
        claimed = self.service.claim_job(["associate"])
        source, target = claimed["materials"]["source"], claimed["materials"]["candidates"][0]
        good = {"target_id": target["id"], "reason_key": "return", "reason": "蓝色围巾归还约定",
                "relation_type": "support", "confidence": .8,
                "evidence": [{"node_id": source["id"], "quote": source["content"]},
                             {"node_id": target["id"], "quote": target["content"]}]}
        bad = {**good, "reason_key": "invented",
               "evidence": [{"node_id": source["id"], "quote": "编造的引用"},
                            {"node_id": target["id"], "quote": target["content"]}]}
        prompts = []
        def complete(messages):
            prompts.append(messages)
            return json.dumps({"links": [good, bad], "prior": .5}, ensure_ascii=False)
        MemoryWorker(self.client, complete, lambda: "")._associate(claimed["job"], claimed["materials"])
        job = self.repo.get_job(claimed["job"]["id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["payload"]["rejected_links"], 1)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges WHERE confidence>0").fetchone()[0], 1)
        view = next(j for j in self.service.list_jobs()["items"] if j["id"] == job["id"])
        self.assertEqual(view["rejected_links"], 1)
        self.assertIsNotNone(view["duration_seconds"])


class WorkerPoolTest(unittest.TestCase):
    def test_four_associations_and_ordinary_work_progress_independently(self):
        lock = threading.Lock()
        release = threading.Event()
        saturated, ordinary, done = threading.Event(), threading.Event(), threading.Event()
        active = peak = completed = 0
        claimed_ids = []
        queue = list(range(1, 10))
        class Client:
            def claim_job(self, kinds=None):
                with lock:
                    if kinds == ["associate"] and queue:
                        jid = queue.pop(0)
                        claimed_ids.append(jid)
                        return {"job": {"id": jid, "kind": "associate", "attempts": 1},
                                "materials": {"source": {"id": "a", "content": "围巾"},
                                              "candidates": [{"id": "b", "content": "围巾"}]}}
                    if kinds and "summarize_experience" in kinds and not ordinary.is_set():
                        ordinary.set()
                        return {"job": {"id": 10, "kind": "summarize_experience", "attempts": 1},
                                "materials": {"record": {}}}
                return None
            def complete_association(self, jid, links, prior, **claim):
                nonlocal completed
                self.assert_attempt(claim)
                with lock:
                    completed += 1
                    if completed == 9:
                        done.set()
            def assert_attempt(self, claim):
                if claim != {"expected_attempt": 1}:
                    raise AssertionError(claim)
            def call(self, method, **params):
                return {"status": "done"}
            def fail_job(self, *args, **params):
                raise AssertionError(args)
        def association(_messages):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 4:
                    saturated.set()
            if not release.wait(3):
                raise AssertionError("关联线程未释放")
            with lock:
                active -= 1
            return '{"links":[]}'
        worker = MemoryWorker(Client(), lambda m: '{"summary":"观察"}', lambda: "",
            association_complete_text=association, association_concurrency=4, idle_seconds=.01)
        worker.start()
        try:
            self.assertTrue(saturated.wait(2))
            self.assertTrue(ordinary.wait(2))
            self.assertEqual(peak, 4)
            release.set()
            self.assertTrue(done.wait(2))
            self.assertEqual(sorted(claimed_ids), list(range(1, 10)))
            self.assertEqual(peak, 4)
        finally:
            release.set()
            worker.stop()
            for thread in worker._threads:
                thread.join(2)
