"""Automatic turn audit through genuine receipts, without correction tool calls."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import Clock, local_env, qq_group_env, qq_private_env
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.worker import MemoryWorker
from test_memory_worker_pool import DirectClient


class TurnAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.tmp.name), state_updates=False,
                           owner_qq_ids=frozenset({"10001"})), self.clock)
        self.service.sync_identities()
        self.client = DirectClient(self.service)
        ids = self.service.record_turn(local_env(self.clock()), "我住在北京", state_update=False)["message_ids"]
        self.raw = self.service.graph.for_message(ids[0])
        self.child = self.repo.add_memory("episode", "owner", "用户居住在首都",
                                         meta={"source_message_ids": ids})
        self.service.graph.register_memory(self.child)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def turn(self, text="你记错了，我一直住在上海，不是北京。", env=None, **extra):
        env = env or local_env(self.clock())
        self.service.record_message(env, "user", text, source_ref="audit-round:user", state_update=False)
        record = {"category": "turn", "status": "completed", "turn_id": "audit-round",
                  "result": "已了解", "source_message_refs": ["audit-round:user"],
                  "turn_message_refs": ["audit-round:user"],
                  "correction_source_refs": ["audit-round:user"],
                  "memory_usage": {"presented": [{"node_id": self.raw["id"],
                      "node_digest": self.raw["digest"], "spans": ["我住在北京"]}], "citations": []}, **extra}
        self.service.record_experience(env, "audit-round:turn", record)
        return record

    def decisions(self, text="你记错了，我一直住在上海，不是北京。"):
        return [{"source": "S0", "verdict": "correction", "quote": "北京", "replacement": "上海",
                 "user": "U0", "correction_quote": text, "explanation": "用户明确否定旧居住地"}]

    def complete(self, claim, decisions=None, approved=None):
        return self.service.complete_memory_audit(claim["job"]["id"],
            decisions if decisions is not None else self.decisions(),
            approved if approved is not None else ["S0"],
            expected_attempt=claim["job"]["attempts"])

    def test_turn_automatically_audits_and_repairs_without_tool_or_manual_intervention(self):
        self.turn()
        responses = iter([{"decisions": self.decisions()}, {"approved": ["S0"]},
                          {"content": "用户一直住在上海"}])
        prompts = []
        def model(messages):
            prompts.append(messages)
            return json.dumps(next(responses), ensure_ascii=False)
        worker = MemoryWorker(self.client, model, lambda: "")
        self.assertTrue(worker.run_once(["audit_memory"]))
        self.assertEqual(len(prompts), 2)
        self.assertTrue(self.repo.get_memory(self.child)["meta"].get("repair_pending"))
        self.assertFalse(self.service.graph.valid(self.service.graph.for_memory(self.child)))
        self.assertTrue(worker.run_once(["repair_memory"]))
        self.assertEqual(self.repo.get_memory(self.child)["content"], "用户一直住在上海")
        self.assertEqual(self.repo.get_memory(self.child)["meta"]["repair_history"][0]["content"], "用户居住在首都")
        self.assertEqual(self.repo.get_message(self.raw["message_ids"][0])["content"], "我住在北京")
        self.assertFalse(worker.run_once(["audit_memory"]))
        self.assertEqual(self.service.corrections.waterline("owner"), 1)

    def test_verifier_can_reject_without_modifying_memory(self):
        self.turn()
        claim = self.service.claim_job(["audit_memory"])
        self.assertEqual(self.complete(claim, approved=[])["status"], "done")
        self.assertEqual(self.service.corrections.waterline("owner"), 0)
        self.assertNotIn("repair_pending", self.repo.get_memory(self.child)["meta"])

    def test_even_two_model_approvals_cannot_correct_an_ambiguous_person(self):
        text = "不是说我，是同事一直住在上海。"
        self.turn(text)
        claim = self.service.claim_job(["audit_memory"])
        result = self.complete(claim, self.decisions(text))
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["decisions"][0]["verdict"], "conflict")
        self.assertFalse(result["decisions"][0]["verified"])
        self.assertEqual(self.service.corrections.waterline("owner"), 0)

    def test_hypothetical_or_negated_correction_cannot_pass_even_if_models_approve(self):
        for revision, text in enumerate(("如果你记错了，我住在上海呢？",
                                        "不是你记错，我以前住北京，现在住上海。"), 1):
            if revision == 1:
                record = self.turn(text)
            else:
                self.repo.conn.execute("UPDATE messages SET content=? WHERE source_ref='audit-round:user'", (text,))
                self.service.record_experience(local_env(self.clock()), "audit-round:turn", record, revision)
            claim = self.service.claim_job(["audit_memory"])
            self.assertEqual(self.complete(claim, self.decisions(text))["status"], "done")
            self.assertEqual(self.service.corrections.waterline("owner"), 0)

    def test_time_change_and_conflict_keep_historical_fact(self):
        record = self.turn("不是记错，我以前住北京，现在搬到上海了。")
        for revision, verdict in enumerate(("time_change", "conflict", "none"), 1):
            with self.subTest(verdict=verdict):
                if revision > 1:
                    self.service.record_experience(local_env(self.clock()), "audit-round:turn",
                                                   record, revision=revision)
                claim = self.service.claim_job(["audit_memory"])
                self.assertEqual(self.complete(claim, [{"source": "S0", "verdict": verdict}], [])["status"], "done")
                self.assertEqual(self.service.corrections.waterline("owner"), 0)

    def test_fabricated_quote_rejected_atomically(self):
        self.turn()
        claim = self.service.claim_job(["audit_memory"])
        for field, value in (("replacement", "深圳"), ("correction_quote", "我住在上海"),
                             ("quote", "住在广州"), ("user", "U99"), ("source", "S99")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.complete(claim, [{**self.decisions()[0], field: value}])
        self.assertEqual(self.service.corrections.waterline("owner"), 0)

    def test_withdrawn_message_or_revision_cannot_commit_late_audit(self):
        record = self.turn()
        claim = self.service.claim_job(["audit_memory"])
        self.service.record_experience(local_env(self.clock()), "audit-round:turn",
                                       {**record, "result": "updated"}, revision=2)
        self.assertEqual(self.complete(claim)["status"], "stale")
        claim = self.service.claim_job(["audit_memory"])
        self.repo.conn.execute("UPDATE messages SET origin='withdrawn' WHERE source_ref='audit-round:user'")
        self.assertEqual(self.complete(claim)["status"], "stale")
        self.assertEqual(self.service.corrections.waterline("owner"), 0)

    def test_changed_source_and_expired_lease_cannot_commit(self):
        self.turn()
        claim = self.service.claim_job(["audit_memory"])
        self.clock.advance(421)
        fresh = self.service.claim_job(["audit_memory"])
        self.assertEqual(self.complete(claim)["status"], "ignored")
        self.service.correct_memory(scope="owner", node_id=self.raw["id"], expected_digest=self.raw["digest"],
            operation_id="newer", kind="fact", quote="北京", replacement="广州", explanation="更新事实")
        self.assertEqual(self.complete(fresh)["status"], "stale")
        self.assertEqual(self.service.corrections.waterline("owner"), 1)

    def test_ordinary_turn_does_not_spend_audit_tokens(self):
        self.turn("谢谢，今天聊得很开心")
        self.assertIsNone(self.service.claim_job(["audit_memory"]))

    def test_untrusted_groups_private_users_and_autonomous_cannot_correct_owner(self):
        for env in (qq_group_env("10001"), qq_private_env("20002"), local_env()):
            with self.subTest(env=env["channel_type"]):
                self.repo.conn.execute("DELETE FROM experiences")
                self.repo.conn.execute("DELETE FROM messages WHERE source_ref='audit-round:user'")
                self.turn(env=env, autonomous=env["channel_type"] == "local")
                self.assertIsNone(self.service.claim_job(["audit_memory"]))
        self.assertEqual(self.service.corrections.waterline("owner"), 0)

    def test_owner_private_binding_rechecked_at_completion(self):
        self.turn(env=qq_private_env("10001"))
        claim = self.service.claim_job(["audit_memory"])
        self.assertIsNotNone(claim)
        self.repo.unlink_identities("qq", "owner", frozenset())
        self.assertEqual(self.complete(claim)["status"], "stale")

    def test_refs_from_another_turn_cannot_trigger_audit(self):
        self.turn(turn_message_refs=["another-round:user"], correction_source_refs=["another-round:user"])
        self.assertIsNone(self.service.claim_job(["audit_memory"]))

    def test_qq_model_cannot_call_background_audit_or_billing_rpc(self):
        import threading
        from chatBot.memory_hub import MemoryHub
        from chatBot.messages import Identity
        hub = MemoryHub.__new__(MemoryHub)
        hub.lock = threading.RLock()
        identities = [Identity("bot", "stranger"), Identity("bot", "owner", owner=True),
                      Identity("bot", "owner", group="group", owner=True, group_role="admin")]
        for identity in identities:
            for method in ("complete_memory_audit", "record_model_call", "claim_job"):
                with self.subTest(scope=identity.scope, method=method), self.assertRaises(PermissionError):
                    hub.call(identity, method, {"scope": "owner", "job_id": 1})

    def test_answer_overlap_is_weak_inferred_activation_without_citation_learning(self):
        text = "我约好下周日带着蓝色围巾去图书馆"
        node = self.service.graph.register_memory(self.repo.add_memory("episode", "owner", text))
        receipt = [{"node_id": node["id"], "node_digest": node["digest"], "spans": [text]}]
        self.service.usage.record("owner", "overlap", receipt, [], text)
        self.service.usage.record("owner", "overlap", receipt, [], text)
        counts = self.service.usage.counts(node["id"])
        self.assertEqual(counts, {"presented": 1, "answer_overlap": 1})
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM context_credit_events").fetchone()[0], 0)
        self.service.usage.record("owner", "unrelated", receipt, [], "另一件事")
        self.assertEqual(self.service.usage.counts(node["id"])["answer_overlap"], 1)


if __name__ == "__main__":
    unittest.main()
