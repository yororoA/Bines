"""Isolated provider errors and explicit admin recovery; no network or real model."""
import errno
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch


class MemoryRetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bines-memory-retry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(self.root / "memory"),
            "BINES_GATEWAY_DIR": str(self.root / "gateway"), "BINES_HEALTH_DIR": str(self.root / "health"),
            "BINES_MODEL_USAGE_PATH": str(self.root / "usage.db"), "BINES_WEBSITE_BRIDGE_SPOOL": str(self.root / "spool"),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        from helpers import Clock
        from memory.lexical_index import LexicalIndex
        from memory.repository import MemoryRepository
        from memory.service import MemoryService
        from memory.settings import MemorySettings
        from test_memory_worker_pool import DirectClient
        self.clock = Clock()
        self.repo = MemoryRepository(self.root / "memory.db", self.clock)
        self.addCleanup(lambda: self.repo.close())
        self.settings = MemorySettings(data_dir=self.root, state_updates=False, associations_enabled=False)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)
        self.client = DirectClient(self.service)
        self.day = self.service._day(self.clock() - 86400)
        self.episode = self.repo.add_memory("episode", "owner", "合成材料：昨天约好周末看电影。",
            day_key=self.day, start_ts=self.clock() - 86400, end_ts=self.clock() - 86400)
        self.service.enqueue_due_diaries()
        self.jid = self.repo.conn.execute("SELECT id FROM jobs WHERE kind='diary'").fetchone()[0]

    @staticmethod
    def wrapped_connection(cause=None):
        import httpx
        from openai import APIConnectionError
        exc = APIConnectionError(request=httpx.Request("POST", "https://synthetic.invalid/private"))
        exc.__cause__ = cause
        return exc

    def worker(self, callback):
        from memory.worker import MemoryWorker
        return MemoryWorker(self.client, callback, lambda: "合成人格",
                            summary_complete_text=lambda _: '{"verdict":"pass","issues":[]}')

    def test_definite_network_failure_retries_beyond_old_limit_and_eventually_writes_one_diary(self):
        import httpx
        from memory.failures import TRANSIENT_DELAYS
        model = Mock(side_effect=self.wrapped_connection(httpx.ConnectError("secret https://private.invalid token")))
        worker = self.worker(model)
        for count in range(1, 21):
            self.assertTrue(worker.run_once(["diary"]))
            job = self.repo.get_job(self.jid)
            self.assertEqual(job["status"], "pending")
            self.assertEqual(job["attempts"], count)
            self.assertEqual(job["payload"]["transient_failures"], count)
            self.assertEqual(job["payload"].get("generation_failures", 0), 0)
            self.assertEqual(job["payload"].get("recovery_cycle", 0), 0)
            self.assertEqual(job["lease_recoveries"], 0)
            self.assertNotIn("terminal_reason", job["payload"])
            self.assertEqual(job["available_at"] - self.clock(), TRANSIENT_DELAYS[min(count, 6) - 1])
            self.assertNotIn("secret", job["last_error"])
            self.assertNotIn("https://", job["last_error"])
            self.assertFalse(worker.run_once(["diary"]))
            self.assertEqual(model.call_count, count)
            self.clock.advance(job["available_at"] - self.clock())
        model.side_effect = None
        model.return_value = "昨天说好了周末看电影。"
        self.assertTrue(worker.run_once(["diary"]))
        self.assertEqual(self.repo.get_job(self.jid)["status"], "done")
        self.assertEqual(len(self.repo.list_memories(kinds=["diary"])), 1)

    def test_cause_chain_distinguishes_transport_from_fixed_wrapped_errors(self):
        import httpx
        from openai import APITimeoutError
        from memory.failures import classify_model_failure, failure_summary
        transient = [ConnectionResetError(), TimeoutError(), ssl.SSLEOFError("private"), ssl.SSLZeroReturnError("private"),
                     httpx.ReadTimeout("private"),
                     httpx.RemoteProtocolError("private"), socket.gaierror(socket.EAI_AGAIN, "private"),
                     OSError(errno.ENETUNREACH, "private"),
                     APITimeoutError(request=httpx.Request("POST", "https://synthetic.invalid"))]
        for cause in transient:
            with self.subTest(cause=type(cause).__name__):
                result = classify_model_failure(self.wrapped_connection(cause))
                self.assertEqual(result["category"], "model_transient")
                self.assertNotIn("private", failure_summary(result))
        for cause in (UnicodeEncodeError("utf8", "\ud800", 0, 1, "private"), ValueError("private"),
                      TypeError("private"), ssl.SSLCertVerificationError("private"),
                      httpx.LocalProtocolError("private"), httpx.InvalidURL("private")):
            with self.subTest(cause=type(cause).__name__):
                self.assertEqual(classify_model_failure(self.wrapped_connection(cause))["category"], "model_request")
        for cause in (FileNotFoundError("private"), socket.gaierror(socket.EAI_NONAME, "private"), ssl.SSLError("private")):
            self.assertNotEqual(classify_model_failure(self.wrapped_connection(cause))["category"], "model_transient")
        self.assertEqual(classify_model_failure(self.wrapped_connection())["category"], "model_unknown")

    def test_http_auth_quota_bad_request_and_recoverable_server_codes_are_distinct(self):
        import httpx
        from openai import APIStatusError
        from memory.failures import classify_model_failure
        for code, expected in ((401, "model_auth"), (403, "model_auth"), (429, "model_quota"),
                               (400, "model_request"), (422, "model_request"), (501, "model_request"),
                               (408, "model_transient"), (500, "model_transient"),
                               (502, "model_transient"), (503, "model_transient"), (504, "model_transient")):
            exc = APIStatusError("secret body", response=httpx.Response(code,
                request=httpx.Request("POST", "https://synthetic.invalid/private")), body={"token": "secret"})
            result = classify_model_failure(exc)
            self.assertEqual((result["category"], result["status_code"]), (expected, code))
            self.assertNotIn("secret", json.dumps(result))

    def test_outer_httpx_connection_wrapper_cannot_hide_fixed_or_unknown_os_cause(self):
        import httpx
        from memory.failures import classify_model_failure
        for cause in (socket.gaierror(socket.EAI_NONAME, "private"), FileNotFoundError("private"),
                      PermissionError("private"), OSError(errno.EINVAL, "private"), OSError("private"),
                      ssl.SSLCertVerificationError("private"), ssl.SSLError("private")):
            outer = httpx.ConnectError("private")
            outer.__cause__ = cause
            with self.subTest(cause=type(cause).__name__):
                self.assertNotEqual(classify_model_failure(self.wrapped_connection(outer))["category"], "model_transient")
        for cause in (socket.gaierror(socket.EAI_AGAIN, "private"), ConnectionRefusedError(),
                      ssl.SSLEOFError("private"), ssl.SSLZeroReturnError("private")):
            outer = httpx.ConnectError("private")
            outer.__cause__ = cause
            self.assertEqual(classify_model_failure(self.wrapped_connection(outer))["category"], "model_transient")

    def test_unknown_wrapped_error_remains_bounded_after_recovery_cycles(self):
        from dataclasses import replace
        self.service.settings = replace(self.settings, job_max_attempts=1)
        model = Mock(side_effect=self.wrapped_connection())
        worker = self.worker(model)
        for cycle in range(3):
            self.assertTrue(worker.run_once(["diary"]))
            job = self.repo.get_job(self.jid)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["payload"]["last_failure"]["category"], "model_unknown")
            self.assertEqual(job["payload"].get("recovery_cycle", 0), cycle)
            self.clock.advance(3601)
        self.assertFalse(worker.run_once(["diary"]))
        self.assertEqual(model.call_count, 3)

    def test_encoding_and_invalid_content_do_not_use_transient_counter(self):
        from dataclasses import replace
        self.service.settings = replace(self.settings, job_max_attempts=2)
        model = Mock(side_effect=self.wrapped_connection(ValueError("bad local request")))
        worker = self.worker(model)
        worker.run_once(["diary"])
        first = self.repo.get_job(self.jid)
        self.assertEqual(first["payload"]["generation_failures"], 1)
        self.assertNotIn("transient_failures", first["payload"])
        self.clock.advance(60)
        model.side_effect = None
        model.return_value = ""
        worker.run_once(["diary"])
        self.assertEqual(self.repo.get_job(self.jid)["status"], "failed")
        self.assertEqual(self.repo.get_job(self.jid)["payload"]["generation_failures"], 2)

    def test_model_usage_rpc_error_is_not_misclassified_as_provider_failure(self):
        from common.model_usage import measured_request
        from memory.client import MemoryUnavailable
        original = self.client.call
        for error in (MemoryUnavailable("offline"), ValueError("lease rejected"), ConnectionResetError("reset")):
            def call(method, **params):
                if method == "record_model_call":
                    raise error
                return original(method, **params)
            self.client.call = Mock(side_effect=call)
            provider = Mock()
            worker = self.worker(lambda messages: measured_request(provider, model="offline", messages=messages))
            with self.assertRaises(type(error)):
                worker.run_once(["diary"])
            provider.assert_not_called()
            job = self.repo.get_job(self.jid)
            self.assertEqual(job["status"], "running")
            self.assertNotIn("generation_failures", job["payload"])
            self.assertNotIn("transient_failures", job["payload"])
            self.clock.advance(301)
        self.client.call = original

    def test_lost_completion_ack_keeps_lease_and_does_not_replay_committed_diary(self):
        from memory.client import MemoryUnavailable
        original = self.client.call
        def call(method, **params):
            result = original(method, **params)
            if method == "complete_diary":
                raise MemoryUnavailable("ack lost")
            return result
        self.client.call = Mock(side_effect=call)
        model = Mock(return_value="合成日记已完成。")
        worker = self.worker(model)
        with self.assertRaises(MemoryUnavailable): worker.run_once(["diary"])
        self.assertEqual(self.repo.get_job(self.jid)["status"], "done")
        self.clock.advance(3601)
        self.assertFalse(worker.run_once(["diary"]))
        self.assertEqual(model.call_count, 1)

    def test_transient_retry_persists_after_restart_and_does_not_revive_deleted_source(self):
        from memory.repository import MemoryRepository
        from memory.service import MemoryService
        from memory.lexical_index import LexicalIndex
        worker = self.worker(Mock(side_effect=TimeoutError("private")))
        worker.run_once(["diary"])
        before = self.repo.get_job(self.jid)
        self.repo.close()
        self.repo = MemoryRepository(self.root / "memory.db", self.clock)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)
        self.assertEqual(self.repo.get_job(self.jid), before)
        self.repo.delete_memory(self.episode)
        self.clock.advance(60)
        self.assertIsNone(self.service.claim_job(["diary"]))
        self.assertEqual(self.repo.get_job(self.jid)["status"], "done")
        self.assertEqual(self.repo.list_memories(kinds=["diary"]), [])

    def failed_job(self):
        self.service.claim_job(["diary"])
        job = self.repo.get_job(self.jid)
        payload = {**job["payload"], "generation_failures": 5, "recovery_cycle": 2}
        self.repo.conn.execute("UPDATE jobs SET status='failed',attempts=15,lease_recoveries=1,lease_until=NULL,"
                              "last_error=?,payload=? WHERE id=?", ("APIConnectionError: Connection error.", json.dumps(payload), self.jid))
        return self.repo.get_job(self.jid)

    def retry_params(self, job):
        return dict(job_id=job["id"], scope=job["scope"], kind=job["kind"], expected_attempt=job["attempts"],
                    expected_updated_at=job["updated_at"], expected_error_type="APIConnectionError", reason="connectivity_rechecked")

    def test_explicit_single_job_retry_preserves_fencing_and_does_not_reset_recovery_history(self):
        job = self.failed_job()
        other = self.repo.enqueue_job("diary", "owner", {"day_key": "2026-01-01"}, "unrelated-failed")
        self.repo.conn.execute("UPDATE jobs SET status='failed',last_error='APIConnectionError: Connection error.' WHERE id=?", (other,))
        untouched = self.repo.get_job(other)
        params = self.retry_params(job)
        result = self.client.retry_failed_job(**params)
        self.assertEqual((result["status"], result["attempts"]), ("pending", 15))
        current = self.repo.get_job(self.jid)
        self.assertEqual((current["attempts"], current["lease_recoveries"]), (15, 1))
        self.assertEqual((current["payload"]["generation_failures"], current["payload"]["recovery_cycle"]), (0, 2))
        self.assertEqual(self.repo.get_job(other), untouched)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_manual_retries").fetchone()[0], 1)
        with self.assertRaises(ValueError): self.client.retry_failed_job(**params)
        fresh = self.service.claim_job(["diary"])["job"]
        self.assertEqual((fresh["id"], fresh["attempts"]), (self.jid, 16))
        self.assertEqual(self.service.complete_diary(self.jid, "过期草稿", expected_attempt=15)["status"], "ignored")

    def test_admin_retry_requires_matching_scope_state_version_error_and_reason(self):
        job = self.failed_job()
        params = self.retry_params(job)
        for key, value in (("scope", "qq_group:other"), ("kind", "associate"), ("expected_attempt", 14),
                           ("expected_updated_at", job["updated_at"] - 1), ("expected_error_type", "ValueError"),
                           ("reason", "https://private.invalid/secret")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.service.retry_failed_job(**{**params, key: value})
        self.assertEqual(self.repo.get_job(self.jid), job)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_manual_retries").fetchone()[0], 0)
        for status in ("done", "pending", "running", "cancelled"):
            self.repo.conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, self.jid))
            with self.assertRaises(ValueError): self.service.retry_failed_job(**params)

    def test_admin_retry_rejects_terminal_lease_budget_and_is_not_available_to_qq(self):
        from chatBot.memory_hub import MemoryHub
        from chatBot.messages import Identity
        from memory.server import dispatch
        job = self.failed_job()
        params = self.retry_params(job)
        hub = MemoryHub.__new__(MemoryHub)
        hub._call = Mock()
        for identity in (Identity("bot", "owner", owner=True), Identity("bot", "owner", group="g", owner=True),
                         Identity("bot", "guest")):
            with self.assertRaises(PermissionError): hub.call(identity, "retry_failed_job", params)
        hub._call.assert_not_called()
        request = {"method": "retry_failed_job", "params": params, "token": "wrong"}
        self.assertFalse(dispatch(self.service, request, token="trusted")["ok"])
        self.assertEqual(self.repo.get_job(self.jid), job)
        self.repo.conn.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps({**job["payload"],
            "terminal_reason": "lease_recovery_budget"}), self.jid))
        with self.assertRaises(ValueError): self.service.retry_failed_job(**params)

    def test_old_unknown_connection_failures_are_not_automatically_revived(self):
        job = self.failed_job()
        self.clock.advance(24 * 3600)
        self.service.maintenance.tick()
        self.assertEqual(self.repo.get_job(self.jid), job)

    def test_safe_failure_rpc_rejects_malformed_metadata_and_ignores_unknown_fields(self):
        from memory.failures import validate_failure
        safe = {"category": "model_transient", "causes": ["APIConnectionError", "ConnectError"]}
        self.assertEqual(validate_failure({**safe, "url": "private", "body": "private"}), safe)
        for bad in ({**safe, "causes": ["https://private"]}, {**safe, "category": "always_retry"},
                    {**safe, "status_code": True}, {**safe, "errno": "private"}):
            with self.assertRaises(ValueError): validate_failure(bad)


if __name__ == "__main__":
    unittest.main()
