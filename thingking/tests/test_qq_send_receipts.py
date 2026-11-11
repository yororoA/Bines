"""Receipt boundaries: proven pre-send failures must not freeze all future activity."""
import copy
from pathlib import Path
import tempfile
import unittest

from chatBot.capabilities import CapabilityService
from chatBot.messages import LOCAL_IDENTITY
from chatBot.receipts import classify_write_result
from chatBot.store import MemoryStore


FAILED = {"status": "failed", "retcode": 1200, "data": None,
          "message": "无法获取用户信息", "wording": "无法获取用户信息"}
PARAMS = {"user_id": "12345", "message": "synthetic"}


class SendReceiptTests(unittest.TestCase):
    def test_generic_send_aliases_keep_owner_contact_cooldown(self):
        from common.proactive import owner_contact
        for action in ("send_msg", "send_msg_async", "send_msg_rate_limited"):
            self.assertTrue(owner_contact(action, {"user_id": "1"}, {"1"}))
            self.assertFalse(owner_contact(action, {"user_id": "2"}, {"1"}))
            self.assertFalse(owner_contact(action, {"group_id": "10"}, {"1"}))

    def test_forward_cooldown_follows_actual_recipient_instead_of_action_name(self):
        from common.proactive import owner_contact
        self.assertTrue(owner_contact("send_forward_msg", {"user_id": "1"}, {"1"}))
        self.assertTrue(owner_contact("send_group_forward_msg", {
            "message_type": "private", "user_id": "1", "group_id": "10"}, {"1"}))
        self.assertFalse(owner_contact("send_private_forward_msg", {
            "message_type": "group", "user_id": "1", "group_id": "10"}, {"1"}))

    def test_exact_pre_send_rejection_remains_failed(self):
        for action in ("send_private_msg", "send_msg"):
            with self.subTest(action=action):
                result = classify_write_result(action, PARAMS, FAILED)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["delivery"], "not_sent")
                self.assertEqual(FAILED["status"], "failed")

    def test_ambiguous_and_partial_results_still_block_resend(self):
        cases = [
            ("send_private_msg", PARAMS, {**FAILED, "retcode": 200}),
            ("send_private_msg", PARAMS, {**FAILED, "message": "timeout", "wording": "timeout"}),
            ("send_private_msg", PARAMS, {**FAILED, "message": "发送后无法获取用户信息"}),
            ("send_private_msg", PARAMS, {**FAILED, "data": {"message_id": 123}}),
            ("send_private_msg", PARAMS, {**FAILED, "data": {"partial": True}}),
            ("send_private_msg", PARAMS, {**FAILED, "message_id": 123}),
            ("send_private_msg", PARAMS, {**FAILED, "success": True}),
            ("send_private_msg", PARAMS, {**FAILED, "delivery": "sent"}),
            ("send_private_msg", {**PARAMS, "group_id": "10"}, FAILED),
            ("send_msg", {**PARAMS, "message_type": "group"}, FAILED),
            ("send_group_msg", {"group_id": "10"}, FAILED),
            ("send_private_forward_msg", PARAMS, FAILED),
            ("new_extension", PARAMS, FAILED),
        ]
        for action, params, receipt in cases:
            with self.subTest(action=action, receipt=receipt, params=params):
                self.assertEqual(classify_write_result(action, params, receipt)["status"], "uncertain")

    def test_success_and_transport_uncertainty_are_unchanged(self):
        for result in ({"status": "ok", "retcode": 0, "data": {"message_id": 1}},
                       {"status": "uncertain", "msg": "connection reset"},
                       {"status": "failed", "retcode": 1400, "message": "invalid parameters"}):
            self.assertEqual(classify_write_result("send_private_msg", PARAMS, result), result)

    def test_failed_operation_is_durable_and_never_redispatched(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "gateway.db", owners={"1"})
            class API:
                calls = 0
                def raw_api(self, *args, **kwargs):
                    self.calls += 1
                    return copy.deepcopy(FAILED)
            api = API()
            service = CapabilityService(api, store, catalog=object())
            first = service.call(LOCAL_IDENTITY, "send_private_msg", PARAMS, "turn:send:0")
            self.assertEqual(first["status"], "failed")
            self.assertEqual(store.operation("turn:send:0"), first)
            restarted = CapabilityService(api, store, catalog=object())
            self.assertEqual(restarted.call(LOCAL_IDENTITY, "send_private_msg", PARAMS, "turn:send:0"), first)
            self.assertEqual(api.calls, 1)
            with self.assertRaises(ValueError):
                restarted.call(LOCAL_IDENTITY, "send_private_msg", {**PARAMS, "user_id": "54321"}, "turn:send:0")


if __name__ == "__main__":
    unittest.main()
