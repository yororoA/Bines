"""Cost controls preserve raw evidence, scope boundaries and automatic recovery."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from helpers import MemoryFixture, local_env, qq_group_env, STRANGER_QQ
from memory.costs import ModelCostStore
from memory.repository import MemoryRepository
from memory.experience import summary_projection
from memory.worker import MemoryWorker


class MemoryEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture(working_segment_extra=2)
        self.addCleanup(self.fx.close)
        self.service, self.repo = self.fx.service, self.fx.repo

    def record(self, ref, *, env=None, **fields):
        record = {"category": "tool", "action": "browser_search", "status": "succeeded",
                  "turn_id": "turn-one", "started_at": self.fx.clock(), "finished_at": self.fx.clock(),
                  "args": {"query": "海鸟"}, "result": f"观察 {ref} https://example.com/birds", **fields}
        return self.service.record_experience(env or local_env(), ref, record)

    def test_projection_removes_runtime_inventory_but_keeps_evidence_and_corrections(self):
        record = {"category": "turn", "status": "interrupted", "result": "未发送的想法",
                  "memory_usage": {"presented": ["重复证据"] * 5000},
                  "memory_diagnostics": {"request": ["计数"] * 5000},
                  "candidate": {"intent": "看海鸟", "activity": {"observations": ["旧状态"] * 5000}},
                  "activity": {"id": "a", "mode": "pause", "reason": "没兴趣了",
                               "observations": [{"action": "web_fetch", "status": "failed", "result": "HTTP 403"}]},
                  "fact_corrections": [{"quote": "昨天", "replacement": "前天"}]}
        projected = summary_projection(record)
        self.assertLess(len(json.dumps(projected)), len(json.dumps(record)) // 10)
        self.assertNotIn("memory_usage", projected)
        self.assertNotIn("memory_diagnostics", projected)
        self.assertEqual(projected["status"], "interrupted")
        self.assertEqual(projected["result"], "未发送的想法")
        self.assertEqual(projected["activity"]["reason"], "没兴趣了")
        self.assertIn("HTTP 403", str(projected["activity"]))
        self.assertEqual(projected["fact_corrections"], record["fact_corrections"])

    def test_raw_receipt_is_not_overwritten_by_projection(self):
        raw = "开头" + "资料" * 10000 + "末尾证据"
        self.record("raw", result=raw, memory_diagnostics={"important_for_audit": True})
        claimed = self.service.claim_job(kinds=["summarize_experience"])
        projected = claimed["materials"]["record"]
        self.assertIn("节选", projected["result"])
        self.assertIn("末尾证据", projected["result"])
        self.assertNotIn("memory_diagnostics", projected)
        original = json.loads(self.repo.conn.execute("SELECT record FROM experiences WHERE source_ref='raw'").fetchone()[0])
        self.assertEqual(original["result"], raw)
        self.assertTrue(original["memory_diagnostics"]["important_for_audit"])

    def test_batch_wait_is_bounded_and_never_mixes_scopes(self):
        self.record("one")
        self.record("two", status="failed", result="网页请求失败")
        self.record("group", env=qq_group_env(STRANGER_QQ))
        self.assertIsNone(self.service.claim_job(kinds=["summarize_experience"], batch_experiences=True))
        self.fx.clock.advance(61)
        claimed = self.service.claim_job(kinds=["summarize_experience"], batch_experiences=True)
        self.assertEqual({item["source_ref"] for item in claimed["materials"]["records"]}, {"one", "two"})
        self.assertEqual(len(claimed["materials"]["batch_jobs"]), 2)
        summaries = {"summaries": [
            {"source_ref": "one", "summary": "看见海鸟。", "tags": ["Bird"]},
            {"source_ref": "two", "summary": "网页请求失败。", "tags": []},
        ]}
        client = Mock()
        client.call.side_effect = lambda method, **params: getattr(self.service, method)(**params)
        model = Mock(return_value=json.dumps(summaries))
        worker = MemoryWorker(client, model, lambda: "")
        worker._experience(claimed["job"], claimed["materials"])
        model.assert_called_once()
        for member in claimed["materials"]["batch_jobs"]:
            self.assertEqual(self.repo.get_job(member["id"])["status"], "done")
            memory = self.repo.get_memory(self.repo.conn.execute(
                "SELECT memory_id FROM experiences WHERE source_ref=?", (member["payload"]["source_ref"],)).fetchone()[0])
            self.assertFalse(memory["meta"]["experience_summary_pending"])
            self.assertEqual(memory["meta"]["source_experience_refs"], [member["payload"]["source_ref"]])
        other = self.service.claim_job(kinds=["summarize_experience"], batch_experiences=True)
        self.assertEqual(other["job"]["scope"], self.service.resolve(qq_group_env(STRANGER_QQ)).scope)

    def test_batch_validates_every_output_before_any_completion(self):
        self.record("one")
        self.record("two")
        self.fx.clock.advance(61)
        claimed = self.service.claim_job(kinds=["summarize_experience"], batch_experiences=True)
        client = Mock()
        worker = MemoryWorker(client, lambda _: '{"summaries":[{"source_ref":"one","summary":"缺少第二项"}]}', lambda: "")
        with self.assertRaises(ValueError):
            worker._experience(claimed["job"], claimed["materials"])
        client.call.assert_not_called()

    def test_batch_completion_does_not_resurrect_revised_or_deleted_sources(self):
        self.record("one")
        deleted = self.record("two")["id"]
        self.record("three")
        self.fx.clock.advance(61)
        claimed = self.service.claim_job(kinds=["summarize_experience"], batch_experiences=True)
        self.service.record_experience(local_env(), "one", {
            "category": "tool", "action": "browser_search", "status": "failed",
            "result": "后来校验失败", "turn_id": "turn-one"}, revision=2)
        self.service.delete_memory(deleted)
        client = Mock()
        client.call.side_effect = lambda method, **params: getattr(self.service, method)(**params)
        output = {"summaries": [{"source_ref": ref, "summary": "旧批次摘要", "tags": []}
                                 for ref in ("one", "two", "three")]}
        MemoryWorker(client, lambda _: json.dumps(output), lambda: "")._experience(
            claimed["job"], claimed["materials"])
        one = self.repo.conn.execute("SELECT memory_id FROM experiences WHERE source_ref='one'").fetchone()[0]
        self.assertIn("后来校验失败", self.repo.get_memory(one)["content"])
        self.assertTrue(self.repo.get_memory(one)["meta"]["experience_summary_pending"])
        self.assertTrue(self.repo.get_memory(deleted)["deleted"])
        three = self.repo.conn.execute("SELECT memory_id FROM experiences WHERE source_ref='three'").fetchone()[0]
        self.assertIn("旧批次摘要", self.repo.get_memory(three)["content"])

    def test_failed_batch_members_retry_individually(self):
        self.record("one")
        self.record("two")
        self.fx.clock.advance(61)
        claimed = self.service.claim_job(kinds=["summarize_experience"], batch_experiences=True)
        worker = MemoryWorker(self.service, lambda _: "invalid JSON", lambda: "")
        worker._run_claimed(claimed["job"], claimed["materials"])
        self.fx.clock.advance(61)
        retried = [self.service.claim_job(kinds=["summarize_experience"], batch_experiences=True)
                   for _ in range(2)]
        self.assertTrue(all("batch_jobs" not in item["materials"] for item in retried))
        self.assertEqual(len({item["job"]["id"] for item in retried}), 2)

    def test_control_receipts_keep_audit_without_paid_summary(self):
        for action in ("activity_continue", "activity_finish", "activity_pause"):
            result = self.record(action, action=action, result={"status": "ok"})
            self.assertTrue(self.repo.get_memory(result["id"])["meta"]["audit_only"])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM experiences").fetchone()[0], 3)
        self.assertIsNone(self.service.claim_job(kinds=["summarize_experience"]))

    def test_two_connections_cannot_spend_the_same_last_budget_slot(self):
        self.record("one")
        self.record("two")
        jobs = [self.service.claim_job(kinds=["summarize_experience"])["job"] for _ in range(2)]
        def reserve(index):
            repo = MemoryRepository(self.fx.settings.db_path, clock=self.fx.clock)
            try:
                store = ModelCostStore(repo)
                with repo.transaction():
                    return store.record(jobs[index], jobs[index]["attempts"], {
                        "call_id": str(index) * 32, "status": "started", "started_at": self.fx.clock(),
                        "estimated_input_units": 100, "max_output_tokens": 20}, daily_limits=True)
            finally:
                repo.close()
        with patch.dict(os.environ, {"BINES_MEMORY_MAINTENANCE_DAILY_CALLS": "1"}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = [item["status"] for item in pool.map(reserve, range(2))]
        self.assertCountEqual(statuses, ["recorded", "deferred"])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_budget_reservations").fetchone()[0], 1)

    def test_worker_defers_every_batch_member_before_calling_provider(self):
        from common.model_usage import measured_request
        self.record("one")
        self.record("two")
        self.fx.clock.advance(61)
        client = Mock()
        client.call.side_effect = lambda method, **params: getattr(self.service, method)(**params)
        client.claim_job.side_effect = lambda **kwargs: self.service.claim_job(batch_experiences=True, **kwargs)
        provider = Mock()
        worker = MemoryWorker(client, lambda messages: measured_request(
            provider, model="test", messages=messages, max_tokens=200), lambda: "")
        with patch.dict(os.environ, {"BINES_MEMORY_MAINTENANCE_DAILY_TOKENS": "1"}):
            self.assertFalse(worker.run_once(kinds=["summarize_experience"]))
        provider.assert_not_called()
        client.fail_job.assert_not_called()
        jobs = self.repo.conn.execute("SELECT status,payload,available_at FROM jobs "
                                      "WHERE kind='summarize_experience'").fetchall()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(row["status"] == "pending" and row["available_at"] > self.fx.clock()
                            and "generation_failures" not in json.loads(row["payload"]) for row in jobs))

    def test_budget_reservation_survives_restart_and_defers_without_generation_failure(self):
        self.record("one")
        job = self.service.claim_job(kinds=["summarize_experience"])["job"]
        event = {"call_id": "a" * 32, "model": "test", "status": "started",
                 "started_at": self.fx.clock(), "estimated_input_units": 100, "max_output_tokens": 20}
        with patch.dict(os.environ, {"BINES_MEMORY_MAINTENANCE_DAILY_CALLS": "1"}):
            self.assertEqual(self.service.record_model_call(job["id"], event, job["attempts"])["status"], "recorded")
            self.service.model_costs = ModelCostStore(self.repo)
            result = self.service.record_model_call(job["id"], {**event, "call_id": "b" * 32}, job["attempts"])
        self.assertEqual(result["status"], "deferred")
        pending = self.repo.get_job(job["id"])
        self.assertEqual(pending["status"], "pending")
        self.assertNotIn("generation_failures", pending["payload"])
        self.assertGreater(pending["available_at"], self.fx.clock())
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_model_calls").fetchone()[0], 1)
        self.assertIsNone(self.repo.conn.execute("SELECT actual_tokens FROM memory_budget_reservations").fetchone()[0])
        self.fx.clock.now = result["retry_at"] + 1
        resumed = self.service.claim_job(kinds=["summarize_experience"])["job"]
        with patch.dict(os.environ, {"BINES_MEMORY_MAINTENANCE_DAILY_CALLS": "1"}):
            self.assertEqual(self.service.record_model_call(resumed["id"], {**event, "call_id": "c" * 32},
                                                           resumed["attempts"])["status"], "recorded")

    def test_token_reservation_reconciles_actual_usage_without_adding_reasoning_twice(self):
        self.record("one")
        job = self.service.claim_job(kinds=["summarize_experience"])["job"]
        event = {"call_id": "a" * 32, "status": "started", "started_at": self.fx.clock(),
                 "estimated_input_units": 100, "max_output_tokens": 30}
        with patch.dict(os.environ, {"BINES_MEMORY_MAINTENANCE_DAILY_TOKENS": "200"}):
            self.service.record_model_call(job["id"], event, job["attempts"])
            self.service.record_model_call(job["id"], {**event, "status": "returned",
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "reasoning_tokens": 8}}, job["attempts"])
            result = self.service.record_model_call(job["id"], {**event, "call_id": "b" * 32}, job["attempts"])
        self.assertEqual(result["status"], "recorded")  # 60 actual + 130 reserved <= 200
        self.assertEqual(self.repo.conn.execute("SELECT actual_tokens FROM memory_budget_reservations "
                                               "WHERE call_id=?", ("a" * 32,)).fetchone()[0], 60)

    def test_history_extends_then_rotates_and_reads_edits_live(self):
        env = self.service.resolve(local_env())
        for index in range(4):
            self.fx.clock.advance(1)
            self.service.record_message(local_env(ts=self.fx.clock()), "user", f"消息{index}")
        first = self.service._working_segment(env)
        for index in range(4, 6):
            self.fx.clock.advance(1)
            self.service.record_message(local_env(ts=self.fx.clock()), "user", f"消息{index}")
        second = self.service._working_segment(env)
        self.assertEqual([row["id"] for row in second[:4]], [row["id"] for row in first])
        self.repo.conn.execute("UPDATE messages SET content='已纠正' WHERE id=?", (first[0]["id"],))
        self.assertEqual(self.service._working_segment(env)[0]["content"], "已纠正")
        self.repo.conn.execute("UPDATE messages SET origin='withdrawn' WHERE id=?", (first[1]["id"],))
        self.assertNotIn(first[1]["id"], [row["id"] for row in self.service._working_segment(env)])
        for index in range(6, 9):
            self.fx.clock.advance(1)
            self.service.record_message(local_env(ts=self.fx.clock()), "user", f"消息{index}")
        third = self.service._working_segment(env)
        self.assertEqual(len(third), 4)
        self.assertEqual(third[-1]["content"], "消息8")


if __name__ == "__main__":
    unittest.main()
