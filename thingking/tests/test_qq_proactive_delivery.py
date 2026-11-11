"""Activity completion is not proof of QQ contact; all receipts are synthetic."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from chatBot.proactive import QQProactive
from chatBot.store import MemoryStore
from common.proactive import ProactiveConfig, ProactivePolicy


class QQProactiveDeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="bines-delivery-")
        self.addCleanup(tmp.cleanup)
        self.store = MemoryStore(Path(tmp.name) / "gateway.db", owners={"1"})
        self.now = 1794310000.0
        self.config = ProactiveConfig(startup_grace=0, quiet_start=0, quiet_end=0)
        self.policy = ProactivePolicy(self.now, self.config)
        self.candidate = {"id": "proactive:synthetic", "kind": "connection", "topic": "synthetic topic",
                          "intent": "synthetic intention", "reason": "synthetic reason",
                          "timestamp": self.now - 1, "based_on_user": "synthetic-user-ref"}
        self.policy.reserve(self.candidate)
        self.api = Mock()
        self.scheduler = QQProactive(self.store, Mock(), self.api, config=self.config)
        self.flags = []
        original = self.policy.resolve
        def observe(*args, **kwargs):
            self.flags.append(dict(self.policy.state.pending))
            return original(*args, **kwargs)
        self.policy.resolve = observe
        self.set_run("completed")

    def set_run(self, status, *, activity="finish", delivered_at=None):
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO proactive_runs VALUES(?,?,?,?,?,?)", (
                self.candidate["id"], "qq:99:p:u:1", json.dumps(self.candidate), status,
                self.now if delivered_at is None else delivered_at, None))
            db.execute("INSERT OR REPLACE INTO turns VALUES(?,?,?)", (
                self.candidate["id"], "done", json.dumps({"activity": {
                    "mode": activity, "topic": "synthetic topic", "intent": "synthetic intention",
                    "delay_seconds": 0, "decision_source": "model",
                }})))

    def operation(self, *, action="send_private_msg", status="ok", user="1", group=None,
                  result=None, params=None):
        params = params if params is not None else ({"user_id": user} if group is None else {"group_id": group})
        result = result if result is not None else {"status": "ok", "retcode": 0, "data": {"message_id": 900}}
        with self.store.connection() as db:
            index = db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)", (
                self.candidate["id"] + f":tool:{index}", action, json.dumps(params), status,
                json.dumps(result), self.now - .1))

    def recover(self):
        self.scheduler.recover(self.policy, self.now)
        self.assertFalse(self.api.mock_calls)
        return self.policy.state

    def assert_no_owner_contact(self, state):
        self.assertEqual(state.sent_at, 0)
        self.assertEqual(state.sent_today, 0)
        self.assertEqual(state.sent_user_ref, "")
        self.assertTrue(state.feedback_done)
        self.assertFalse(self.flags[-1]["owner_contacted"])

    def test_failed_send_then_activity_finish_is_not_delivered_contact(self):
        self.operation(status="failed", result={"status": "failed", "retcode": 1200,
                                                "wording": "synthetic pre-send failure", "data": None})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertFalse(self.flags[-1]["contacted"])
        self.assertEqual(state.recent_outcomes[-1]["outcome"], "completed")
        self.assertEqual(state.last_executed_at, self.now)

    def test_activity_without_any_receipt_completes_without_contact(self):
        self.set_run("completed", delivered_at=0)
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.recent_outcomes[-1]["outcome"], "completed")

    def test_successful_non_send_operation_is_not_contact(self):
        for action in ("get_status", "send_like", "set_friend_remark"):
            with self.subTest(action=action):
                self.operation(action=action)
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertFalse(self.flags[-1]["contacted"])
        self.assertEqual(state.recent_outcomes[-1]["outcome"], "completed")

    def test_successful_other_private_or_group_send_does_not_contact_owner(self):
        self.operation(user="2")
        self.operation(action="send_group_msg", group="10")
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertTrue(self.flags[-1]["contacted"])

    def test_verified_owner_send_advances_contact_once_even_if_later_run_failed(self):
        self.operation()
        self.set_run("failed", delivered_at=0)
        state = self.recover()
        self.assertEqual(state.sent_today, 1)
        self.assertEqual(state.sent_user_ref, self.candidate["based_on_user"])
        self.assertGreater(state.sent_at, 0)
        self.assertFalse(state.feedback_done)
        self.assertTrue(self.flags[-1]["owner_contacted"])
        before = state.sent_at
        self.scheduler.recover(self.policy, self.now + 1)
        self.assertEqual(state.sent_today, 1)
        self.assertEqual(state.sent_at, before)

    def test_ok_envelope_with_nonzero_code_is_not_verified_send(self):
        self.operation(result={"status": "ok", "retcode": 1200, "data": {"message_id": 900}})
        self.assert_no_owner_contact(self.recover())

    def test_boolean_or_missing_retcode_cannot_prove_delivery(self):
        self.operation(result={"status": "ok", "retcode": False, "data": {"message_id": 900}})
        self.operation(result={"status": "ok", "data": {"message_id": 901}})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.uncertain_contact_ref, self.candidate["based_on_user"])

    def test_ok_without_message_id_is_uncertain_not_contact_success(self):
        self.operation(result={"status": "ok", "retcode": 0, "data": None})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.uncertain_contact_ref, self.candidate["based_on_user"])
        self.assertEqual(state.recent_outcomes[-1]["outcome"], "uncertain")
        self.assertEqual(state.activity["mode"], "pause")

    def test_uncertain_owner_send_blocks_repeat_without_claiming_delivery(self):
        self.operation(status="uncertain", result={"status": "uncertain", "data": None})
        self.set_run("uncertain", activity="pause")
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.uncertain_contact_ref, self.candidate["based_on_user"])
        self.assertEqual(state.activity["mode"], "pause")
        self.assertEqual(state.recent_outcomes[-1]["outcome"], "uncertain")

    def test_uncertain_public_send_does_not_claim_uncertain_owner_contact(self):
        self.operation(action="send_group_msg", group="10", status="uncertain",
                       result={"status": "uncertain", "data": None})
        self.set_run("uncertain", activity="pause")
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.uncertain_contact_ref, "")
        self.assertEqual(state.activity["mode"], "pause")

    def test_historical_send_alias_needs_message_id_before_contact_success(self):
        self.operation(action="send_private_msg_async", result={"status": "ok", "retcode": 0, "data": None})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertFalse(self.flags[-1]["contacted"])
        self.assertEqual(state.uncertain_contact_ref, self.candidate["based_on_user"])
        self.assertEqual(state.activity["mode"], "pause")

    def test_historical_uncertain_rate_limited_send_preserves_contact_guard(self):
        self.operation(action="send_private_msg_rate_limited", status="uncertain",
                       result={"status": "uncertain", "data": None})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.uncertain_contact_ref, self.candidate["based_on_user"])

    def test_generic_private_forward_success_counts_owner_contact(self):
        self.operation(action="send_forward_msg", params={"message_type": "private", "user_id": "1"})
        state = self.recover()
        self.assertEqual(state.sent_today, 1)
        self.assertTrue(self.flags[-1]["owner_contacted"])

    def test_private_forward_overridden_to_group_does_not_contact_owner(self):
        self.operation(action="send_private_forward_msg", params={
            "message_type": "group", "group_id": "10", "user_id": "1"})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertTrue(self.flags[-1]["contacted"])

    def test_group_forward_overridden_to_private_keeps_uncertain_owner_guard(self):
        self.operation(action="send_group_forward_msg", status="uncertain", params={
            "message_type": "private", "group_id": "10", "user_id": "1"},
            result={"status": "uncertain", "data": None})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.uncertain_contact_ref, self.candidate["based_on_user"])

    def test_confirmed_private_send_with_group_fallback_does_not_claim_owner_delivery(self):
        self.operation(params={"user_id": "1", "group_id": "10"})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertTrue(self.flags[-1]["contacted"])
        self.assertEqual(state.recent_outcomes[-1]["outcome"], "completed")
        self.assertEqual(state.uncertain_contact_ref, self.candidate["based_on_user"])

    def test_completed_run_cannot_hide_started_send_receipt(self):
        self.operation(status="started", result={})
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.uncertain_contact_ref, self.candidate["based_on_user"])
        self.assertEqual(state.activity["mode"], "pause")

    def test_confirmed_owner_send_and_uncertain_other_action_keep_distinct_facts(self):
        self.operation()
        self.operation(action="set_friend_remark", status="uncertain", result={"status": "uncertain"})
        self.set_run("uncertain", activity="pause")
        state = self.recover()
        self.assertEqual(state.sent_today, 1)
        self.assertGreater(state.sent_at, 0)
        self.assertEqual(state.uncertain_contact_ref, "")
        self.assertEqual(state.recent_outcomes[-1]["outcome"], "uncertain")
        self.assertEqual(state.activity["mode"], "pause")

    def test_curiosity_activity_completion_and_interest_are_preserved(self):
        self.policy.state.pending["kind"] = "curiosity"
        with self.store.connection() as db:
            payload = json.loads(db.execute("SELECT payload FROM turns").fetchone()[0])
            payload["activity"]["decision"] = {"interest_level": .8, "reason": "a grounded synthetic observation"}
            db.execute("UPDATE turns SET payload=?", (json.dumps(payload),))
        state = self.recover()
        self.assert_no_owner_contact(state)
        self.assertEqual(state.actions_today, 1)
        self.assertEqual(state.recent_outcomes[-1]["outcome"], "completed")
        self.assertEqual(state.recent_outcomes[-1]["interest_level"], .8)
        self.assertIn("synthetic topic", state.interests)


if __name__ == "__main__":
    unittest.main()
