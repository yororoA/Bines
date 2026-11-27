"""HTTP memory transport, uncertain receipts and bounded model-output correction."""
import copy
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock

from chatBot.gateway import GatewayHandler
from chatBot.gateway_client import GatewayClient
from chatBot.memory_hub import GatewayMemoryClient
from memory.client import MemoryUnavailable
from memory.worker import MemoryWorker, _validate_state_changes
import test_compact_association_repair as compact_tests


class MemoryTransportTests(unittest.TestCase):
    def setUp(self):
        self.hub = Mock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        self.server.service, self.server.memory_hub, self.server.token = Mock(), self.hub, "isolated-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.gateway = GatewayClient(f"http://127.0.0.1:{self.server.server_port}", token="isolated-token")
        self.client = GatewayMemoryClient(self.gateway)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_claim_timeout_retains_transport_type(self):
        self.hub.call.side_effect = MemoryUnavailable("private endpoint")
        with self.assertRaises(MemoryUnavailable) as caught:
            self.client.claim_job()
        self.assertNotIn("private endpoint", str(caught.exception))
        self.assertEqual(self.hub.call.call_count, 1)

    def test_lost_completion_never_fails_or_replays_job(self):
        committed = []
        def call(identity, method, params, **kwargs):
            committed.append(method)
            raise MemoryUnavailable("reply lost after commit")
        self.hub.call.side_effect = call
        self.client.fail_job = Mock()
        worker = MemoryWorker(self.client, Mock(), lambda: "")
        worker._associate = lambda job, materials: self.client.complete_association(job["id"], [], expected_attempt=1)
        with self.assertRaises(MemoryUnavailable):
            worker._run_claimed({"id": 1, "kind": "associate", "attempts": 1}, {})
        self.assertEqual(committed, ["complete_association"])
        self.client.fail_job.assert_not_called()

    def test_application_error_is_not_treated_as_transport_failure(self):
        self.hub.call.side_effect = RuntimeError("Memory Server complete_association failed: ValueError: invalid evidence")
        with self.assertRaises(RuntimeError) as caught:
            self.client.complete_association(1, [])
        self.assertNotIsInstance(caught.exception, MemoryUnavailable)
        self.assertIn("invalid evidence", str(caught.exception))

    def test_permission_error_stays_denied(self):
        self.hub.call.side_effect = PermissionError("wrong scope")
        with self.assertRaises(RuntimeError) as caught:
            self.client.claim_job()
        self.assertNotIsInstance(caught.exception, MemoryUnavailable)
        self.assertIn("wrong scope", str(caught.exception))


class StateOutputCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.complete_state_update.return_value = {"changed": True}
        self.job = {"id": 1, "kind": "update_state", "scope": "owner", "attempts": 3}
        self.materials = {"state": {}, "messages": [{"content": "我已到家"}], "version": 2}

    def worker(self, answers):
        prompts = []
        answers = iter(answers)
        def model(messages):
            prompts.append(copy.deepcopy(messages))
            return next(answers)
        return MemoryWorker(self.client, Mock(), lambda: "", state_complete_text=model), prompts

    def test_unknown_fields_are_corrected_before_any_rpc(self):
        worker, prompts = self.worker(['{"relationship_level":"亲密","current_location":"家"}',
                                       '{"current_location":"家"}'])
        worker._update_state(self.job, self.materials)
        self.assertEqual(len(prompts), 2)
        self.assertIn("校验未通过", prompts[1][-1]["content"])
        self.client.complete_state_update.assert_called_once_with(
            1, {"current_location": "家"}, expected_version=2, expected_attempt=3)

    def test_invalid_json_gets_one_correction(self):
        worker, prompts = self.worker(['{"current_location":"家" "add_item":"围巾"}', '{}'])
        worker._update_state(self.job, self.materials)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(self.client.complete_state_update.call_args.args[1], {})

    def test_two_bad_answers_never_write(self):
        worker, prompts = self.worker(['{"state":{}}', '{"state":{}}'])
        with self.assertRaises(ValueError):
            worker._update_state(self.job, self.materials)
        self.assertEqual(len(prompts), 2)
        self.client.complete_state_update.assert_not_called()

    def test_singleton_string_compatibility_keeps_input_intact(self):
        original = {"add_item": ["围巾"], "npc_activity": None, "relationship_delta": 0}
        self.assertEqual(_validate_state_changes(original), {"add_item": "围巾", "relationship_delta": 0})
        self.assertEqual(original["add_item"], ["围巾"])

    def test_invalid_delta_and_text_types_are_rejected(self):
        for changes in ({"relationship_delta": True}, {"relationship_delta": 3},
                        {"relationship_delta": "1"}, {"add_item": ["a", "b"]},
                        {"npc_activity": {}}, {"npc_activity": 3}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _validate_state_changes(changes)

    def test_uncertain_state_commit_never_repeats_model_or_rpc(self):
        self.client.complete_state_update.side_effect = MemoryUnavailable("lost state receipt")
        worker, prompts = self.worker(['{"relationship_delta":1}'])
        with self.assertRaises(MemoryUnavailable):
            worker._update_state(self.job, self.materials)
        self.assertEqual(len(prompts), 1)
        self.client.complete_state_update.assert_called_once()


class AssociationVerificationCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = compact_tests.CompactAssociationRepairTest("test_missing_candidate_gets_feedback_and_completes_full_page")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def model(self, verify_answers):
        from memory.association_plan import VERIFY_SYSTEM
        answers, calls = iter(verify_answers), {"judge": 0, "verify": 0}
        def complete(messages):
            phase = "verify" if messages[0]["content"] == VERIFY_SYSTEM else "judge"
            calls[phase] += 1
            return next(answers) if phase == "verify" else self.fixture.answer(self.fixture.decisions())
        return complete, calls

    def valid(self):
        return json.dumps({"verdicts": [{"target": d["target"], "supported": True}
                                       for d in self.fixture.decisions()]})

    def test_malformed_verification_is_corrected_before_committing(self):
        complete, calls = self.model(['{"verdicts":[{"target":"t1","supported":true} {"target":"t2","supported":true}]}',
                                      self.valid()])
        worker = MemoryWorker(self.fixture.client, complete, lambda: "")
        worker._associate(self.fixture.claim["job"], self.fixture.claim["materials"])
        self.assertEqual(calls, {"judge": 2, "verify": 2})
        self.assertEqual(self.fixture.repo.get_job(self.fixture.claim["job"]["id"])["status"], "done")

    def test_incomplete_verification_is_corrected(self):
        complete, calls = self.model(['{"verdicts":[]}', self.valid()])
        worker = MemoryWorker(self.fixture.client, complete, lambda: "")
        worker._associate(self.fixture.claim["job"], self.fixture.claim["materials"])
        self.assertEqual(calls["verify"], 2)
        self.assertEqual(self.fixture.repo.get_job(self.fixture.claim["job"]["id"])["status"], "done")

    def test_two_bad_verifications_cannot_commit_unverified_links(self):
        complete, calls = self.model(['{"verdicts":[]}', '{"verdicts":[]}'])
        worker = MemoryWorker(self.fixture.client, complete, lambda: "")
        with self.assertRaises(ValueError):
            worker._associate(self.fixture.claim["job"], self.fixture.claim["materials"])
        self.fixture.assert_uncommitted()
        self.assertEqual(calls, {"judge": 2, "verify": 2})


class LifeGoalQueryRegressionTests(unittest.TestCase):
    def setUp(self):
        from memory.repository import MemoryRepository
        from memory.life import LifeLedger
        self.now = 1_794_500_000
        self.repo = MemoryRepository(":memory:", lambda: self.now)
        self.addCleanup(self.repo.close)
        self.repo.conn.execute("""CREATE TABLE experiences (
            source_ref TEXT PRIMARY KEY, memory_id TEXT, scope TEXT, record TEXT,
            revision INTEGER, recorded_at REAL)""")
        self.life = LifeLedger(self.repo, lambda: self.now)

    def add_event(self, ref, *, scope="owner", event_ref=None, ts=None):
        ts = self.now if ts is None else ts
        record = {"autonomous": True, "category": "turn", "action": "autonomous_turn",
                  "candidate": {"id": ref, "topic": ref}, "status": "completed", "finished_at": ts}
        self.life._put_event(event_ref or "life:" + ref, scope, "experience", ts, {"record": record})

    def test_goal_scan_stays_bounded_when_unrelated_experiences_grow(self):
        for index in range(500):
            self.add_event(f"visible:{index}")
        self.repo.conn.executemany("INSERT INTO experiences VALUES(?,NULL,'other','{}',1,?)",
            [(f"unrelated:{index}", self.now) for index in range(3000)])
        steps = 0
        def budget():
            nonlocal steps
            steps += 1000
            return int(steps > 250_000)
        self.repo.conn.set_progress_handler(budget, 1000)
        try:
            goals = self.life._goals("owner", self.now)
        finally:
            self.repo.conn.set_progress_handler(None, 0)
        self.assertEqual(len(goals), 20)
        self.assertLessEqual(steps, 250_000)

    def test_scope_time_deleted_and_orphan_visibility_are_preserved(self):
        active = self.repo.add_memory("episode", "owner", "有效来源")
        deleted = self.repo.add_memory("episode", "owner", "失效来源")
        self.repo.delete_memory(deleted)
        self.repo.conn.executemany("INSERT INTO experiences VALUES(?,?,'owner','{}',1,?)",
            [("visible", active, self.now), ("deleted", deleted, self.now)])
        for ref in ("visible", "deleted", "orphan"):
            self.add_event(ref)
        self.add_event("stranger", scope="other")
        self.add_event("future", ts=self.now + 60)
        titles = {goal["title"] for goal in self.life._goals("owner", self.now)}
        self.assertEqual(titles, {"visible", "orphan"})

    def test_event_prefix_remains_exact_with_nested_and_nonstandard_refs(self):
        deleted = self.repo.add_memory("episode", "owner", "删除的来源")
        self.repo.delete_memory(deleted)
        self.repo.conn.executemany("INSERT INTO experiences VALUES(?,?,'owner','{}',1,?)",
            [("hidden", deleted, self.now), ("life:nested", deleted, self.now)])
        self.add_event("hidden")
        self.add_event("nested", event_ref="life:life:nested")
        self.add_event("unmatched", event_ref="otherhidden")
        self.add_event("case-sensitive", event_ref="Life:hidden")
        self.assertEqual({goal["title"] for goal in self.life._goals("owner", self.now)},
                         {"unmatched", "case-sensitive"})


if __name__ == "__main__":
    unittest.main()
