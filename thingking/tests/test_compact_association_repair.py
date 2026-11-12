"""Bounded compact-answer correction against the real in-memory validator."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import Clock
from memory.client import MemoryClient, MemoryUnavailable
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.worker import MemoryWorker


class ServiceClient(MemoryClient):
    """Use the actual service with the two production RPC error envelopes."""

    def __init__(self, service, embedded=False):
        self.service = service
        self.embedded = embedded

    def call(self, method, **params):
        try:
            return getattr(self.service, method)(**params)
        except ValueError as exc:
            prefix = "" if self.embedded else f"Memory Server {method} failed: "
            raise RuntimeError(f"{prefix}ValueError: {exc}") from exc


class CompactAssociationRepairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bines_compact_repair_")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        environment = patch.dict("os.environ", {
            "BINES_ENV_FILE": "/dev/null",
            "BINES_MEMORY_DIR": str(root),
            "BINES_GATEWAY_DIR": str(root / "gateway"),
            "BINES_HEALTH_DIR": str(root / "health"),
            "BINES_WEBSITE_BRIDGE_SPOOL": str(root / "spool"),
            "BINES_MODEL_USAGE_PATH": str(root / "model_usage.db"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=root, state_updates=False, association_backfill_batch=0), self.clock)
        self.client = ServiceClient(self.service)
        for content in ("蓝色围巾约定周六归还", "蓝色围巾改成周日归还", "蓝色围巾又改为周一归还"):
            mid = self.repo.add_memory("episode", "owner", content)
            self.service.graph.register_memory(mid)
        self.claim = self.service.claim_job(["associate"])
        self.assertEqual(len(self.claim["materials"]["compact"]["candidates"]), 2)

    def decisions(self):
        compact = self.claim["materials"]["compact"]
        return [{"target": card["id"], "links": [{
            "reason_key": "scarf-return", "reason": "蓝色围巾归还日期变化",
            "relation_type": "contradicts", "confidence": .8,
            "evidence_ids": [compact["source"]["evidence"][0]["id"], card["evidence"][0]["id"]],
        }]} for card in compact["candidates"]]

    def wrong_evidence(self):
        decisions = self.decisions()
        decisions[0]["links"][0]["evidence_ids"][1] = decisions[1]["links"][0]["evidence_ids"][1]
        return decisions

    def answer(self, decisions):
        return json.dumps({"prior": .6, "decisions": decisions}, ensure_ascii=False)

    def assert_uncommitted(self):
        for table in ("association_edges", "association_decisions"):
            self.assertEqual(self.repo.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        job = self.repo.get_job(self.claim["job"]["id"])
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["attempts"], 1)
        self.assertFalse(job["payload"].get("generation_failures"))
        node = self.service.graph.get(job["payload"]["node_id"])
        self.assertEqual(node["ready"], 0)

    def worker(self, answers):
        replies = iter(answers)
        prompts = []

        def model(messages):
            from memory.association_plan import VERIFY_SYSTEM
            if messages[0]["content"] == VERIFY_SYSTEM:
                return json.dumps({"verdicts": [{"target": d["target"], "supported": True}
                    for d in json.loads(messages[-1]["content"])["proposed"]]})
            # A rejected full page must not commit even its individually valid links.
            self.assert_uncommitted()
            prompts.append(copy.deepcopy(messages))
            return next(replies)

        return MemoryWorker(self.client, model, lambda: ""), prompts

    def run_worker(self, worker):
        worker._associate(self.claim["job"], self.claim["materials"])

    def assert_repaired(self, prompts, feedback):
        self.assertEqual(len(prompts), 2)
        self.assertEqual(len(prompts[1]), 4)
        self.assertIn(feedback, prompts[1][-1]["content"])
        targets = [card["id"] for card in self.claim["materials"]["compact"]["candidates"]]
        self.assertIn("本次必须判断的 target：" + json.dumps(targets), prompts[1][-1]["content"])
        self.assertEqual(prompts[0][:2], prompts[1][:2])
        self.assertEqual(self.repo.get_job(self.claim["job"]["id"])["status"], "done")
        for table in ("association_edges", "association_decisions"):
            self.assertEqual(self.repo.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 2)

    def test_missing_candidate_gets_feedback_and_completes_full_page(self):
        worker, prompts = self.worker([self.answer(self.decisions()[:1]), self.answer(self.decisions())])
        self.run_worker(worker)
        self.assert_repaired(prompts, "每个候选必须显式判断")

    def test_wrong_pair_evidence_recovers_with_embedded_error_envelope(self):
        self.client.embedded = True
        worker, prompts = self.worker([self.answer(self.wrong_evidence()), self.answer(self.decisions())])
        self.run_worker(worker)
        self.assert_repaired(prompts, "建联证据必须且只能来自双方授权材料")

    def test_two_rejections_do_not_write_or_cache_any_partial_decisions(self):
        worker, prompts = self.worker([self.answer(self.decisions()[:1]), self.answer(self.wrong_evidence())])
        with self.assertRaisesRegex(RuntimeError, "建联证据必须且只能来自双方授权材料"):
            self.run_worker(worker)
        self.assertEqual(len(prompts), 2)
        self.assert_uncommitted()

    def test_feedback_requires_only_uncached_targets(self):
        # A bounded-cache eviction can leave one candidate cached and another missing.
        plan = self.claim["job"]["payload"]["association_plan"]
        decisions = [{"target": alias, "links": []} for alias in plan["keys"]]
        self.service.complete_association(self.claim["job"]["id"], [], decisions=decisions)
        cached_alias, missing_alias = list(plan["keys"])
        self.repo.conn.execute("DELETE FROM association_decisions WHERE cache_key=?", (plan["keys"][missing_alias],))
        node = self.service.graph.get(self.claim["job"]["payload"]["node_id"])
        jid = self.repo.enqueue_job("associate", "owner", {"node_id": node["id"], "revision": node["revision"]},
                                   "reassess-with-partial-cache")
        self.repo.conn.execute("UPDATE jobs SET status='done' WHERE status='pending' AND id!=?", (jid,))
        self.claim = self.service.claim_job(["associate"])
        self.assertEqual([card["id"] for card in self.claim["materials"]["compact"]["candidates"]], [missing_alias])
        prompts = []

        def model(messages):
            prompts.append(copy.deepcopy(messages))
            self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_decisions").fetchone()[0], 1)
            return self.answer([] if len(prompts) == 1 else [{"target": missing_alias, "links": []}])

        self.run_worker(MemoryWorker(self.client, model, lambda: ""))
        self.assertEqual(len(prompts), 2)
        feedback = prompts[1][-1]["content"]
        self.assertIn("本次必须判断的 target：" + json.dumps([missing_alias]), feedback)
        self.assertNotIn(cached_alias, feedback)
        self.assertEqual(self.repo.get_job(jid)["status"], "done")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_decisions").fetchone()[0], 2)

    def test_json_parse_failure_gets_one_correction(self):
        worker, prompts = self.worker(['{"decisions":', self.answer(self.decisions())])
        self.run_worker(worker)
        self.assert_repaired(prompts, "JSON is invalid")

    def test_legacy_links_cannot_bypass_explicit_compact_decisions(self):
        worker, prompts = self.worker(['{"links": []}', self.answer(self.decisions())])
        self.run_worker(worker)
        self.assert_repaired(prompts, "decisions 列表")

    def test_transport_errors_never_trigger_another_model_call(self):
        for error in (MemoryUnavailable("server restarting"), ConnectionError("disconnected"), TimeoutError("timeout")):
            with self.subTest(error=type(error).__name__):
                worker, prompts = self.worker([self.answer(self.decisions())])
                with patch.object(self.client, "complete_association", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.run_worker(worker)
                self.assertEqual(len(prompts), 1)
                self.assert_uncommitted()

    def test_unrelated_rpc_errors_never_trigger_another_model_call(self):
        for error in (RuntimeError("Memory Server complete_association failed: OperationalError: busy"),
                      RuntimeError("server unavailable; previous error ValueError: invalid"),
                      ValueError("invalid RPC reply JSON")):
            with self.subTest(error=str(error)):
                worker, prompts = self.worker([self.answer(self.decisions())])
                with patch.object(self.client, "complete_association", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.run_worker(worker)
                self.assertEqual(len(prompts), 1)
                self.assert_uncommitted()

    def test_model_call_errors_never_enter_answer_correction(self):
        from memory.failures import ModelFailure
        for error in (ValueError("model client failed"), MemoryUnavailable("upstream unavailable")):
            with self.subTest(error=type(error).__name__):
                calls = []

                def model(messages):
                    calls.append(messages)
                    raise error

                worker = MemoryWorker(self.client, model, lambda: "")
                with self.assertRaises(MemoryUnavailable if isinstance(error, MemoryUnavailable) else ModelFailure):
                    self.run_worker(worker)
                self.assertEqual(len(calls), 1)
                self.assert_uncommitted()


if __name__ == "__main__":
    unittest.main()
