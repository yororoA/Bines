"""Gateway restarts: bounded polling, quiet logs and uncertain-write lease safety."""
import json
from concurrent.futures import ThreadPoolExecutor
from http.client import RemoteDisconnected
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from helpers import Clock
from memory.client import MemoryUnavailable
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.worker import MemoryWorker
from test_memory_worker_pool import DirectClient


class RecordingStop:
    """Advance a deterministic loop without sleeping or touching a real gateway."""
    def __init__(self, waits):
        self.limit, self.delays = waits, []

    def is_set(self):
        return len(self.delays) >= self.limit

    def wait(self, delay):
        self.delays.append(delay)
        return self.is_set()


class WorkerDisconnectTests(unittest.TestCase):
    def worker(self, client=None, **kwargs):
        return MemoryWorker(client or Mock(), Mock(), lambda: "", **kwargs)

    def test_transport_failures_back_off_even_with_zero_idle_interval(self):
        client = Mock()
        client.claim_job.side_effect = RemoteDisconnected("response lost")
        worker = self.worker(client, idle_seconds=0)
        worker._stop = RecordingStop(8)
        with patch("memory.worker.logger") as logger:
            worker._loop()
        self.assertEqual(worker._stop.delays, [1, 2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(client.claim_job.call_count, 8)
        logger.warning.assert_called_once()
        logger.exception.assert_not_called()
        worker.complete_text.assert_not_called()
        client.fail_job.assert_not_called()

    def test_successful_poll_resets_backoff(self):
        client = Mock()
        client.claim_job.side_effect = [MemoryUnavailable("offline"), MemoryUnavailable("offline"),
                                       None, MemoryUnavailable("offline again"), None]
        worker = self.worker(client, idle_seconds=.25)
        worker._stop = RecordingStop(5)
        with patch("memory.worker.logger"):
            worker._loop()
        self.assertEqual(worker._stop.delays, [1, 2, .25, 1, .25])

    def test_five_simultaneous_failures_share_one_warning_window(self):
        worker = self.worker()
        barrier = threading.Barrier(5)
        def disconnected(_):
            barrier.wait(timeout=2)
            worker._warn_transport(RemoteDisconnected("private endpoint must not appear"), 2)
        with patch("memory.worker.logger") as logger, patch("memory.worker.time.monotonic", return_value=10):
            with ThreadPoolExecutor(max_workers=5) as pool:
                list(pool.map(disconnected, range(5)))
            logger.warning.assert_called_once()
            self.assertEqual(worker._transport_suppressed, 4)
            message = logger.warning.call_args.args[0] % logger.warning.call_args.args[1:]
            self.assertNotIn("private endpoint", message)
            self.assertIn("RemoteDisconnected", message)
        with patch("memory.worker.logger") as logger, patch("memory.worker.time.monotonic", return_value=40):
            worker._warn_transport(MemoryUnavailable("still offline"), 30)
            logger.warning.assert_called_once()
            self.assertEqual(logger.warning.call_args.args[-1], 4)

    def test_unknown_failures_keep_tracebacks_and_do_not_busy_loop(self):
        client = Mock()
        client.claim_job.side_effect = ValueError("invalid reply")
        worker = self.worker(client, idle_seconds=0)
        worker._stop = RecordingStop(2)
        with patch("memory.worker.logger") as logger:
            worker._loop()
        self.assertEqual(worker._stop.delays, [1, 2])
        self.assertEqual(logger.exception.call_count, 2)
        logger.warning.assert_not_called()

    def test_known_transport_errors_do_not_hide_http_or_local_file_errors(self):
        for exc in (MemoryUnavailable("offline"), RemoteDisconnected(), ConnectionResetError(),
                    TimeoutError(), URLError("offline")):
            with self.subTest(error=type(exc).__name__):
                self.assertTrue(MemoryWorker._transport_error(exc))
        for exc in (ValueError(), FileNotFoundError(), HTTPError("http://local", 403, "denied", {}, None)):
            with self.subTest(error=type(exc).__name__):
                self.assertFalse(MemoryWorker._transport_error(exc))

    def test_stop_interrupts_long_transport_backoff(self):
        client = Mock()
        client.claim_job.side_effect = MemoryUnavailable("offline")
        worker = self.worker(client, idle_seconds=30)
        warned = threading.Event()
        with patch("memory.worker.logger.warning", side_effect=lambda *args: warned.set()):
            worker.start()
            try:
                self.assertTrue(warned.wait(2))
                worker.stop()
                worker._thread.join(.5)
                self.assertFalse(worker.alive)
                self.assertEqual(client.claim_job.call_count, 1)
            finally:
                worker.stop()
                worker._thread.join(2)


class WorkerUncertainWriteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.temporary.name), state_updates=False), self.clock)
        for content in ("周六归还蓝色围巾", "蓝色围巾改为周日归还"):
            mid = self.repo.add_memory("episode", "owner", content)
            self.service.graph.register_memory(mid)

    @staticmethod
    def model(messages):
        compact = json.loads(messages[-1]["content"])
        return json.dumps({"prior": .5, "decisions": [
            {"target": card["id"], "links": []} for card in compact["candidates"]]})

    def client(self, method, *, committed):
        client = DirectClient(self.service)
        original = client.call
        def call(operation, **params):
            if operation != method:
                return original(operation, **params)
            if committed:
                original(operation, **params)
            raise MemoryUnavailable("response lost during recreation")
        client.call = Mock(side_effect=call)
        client.fail_job = Mock(wraps=client.fail_job)
        return client

    def test_claim_response_loss_keeps_committed_lease(self):
        client = self.client("claim_job", committed=True)
        model = Mock(side_effect=self.model)
        worker = MemoryWorker(client, model, lambda: "")
        with self.assertRaises(MemoryUnavailable):
            worker.run_once(["associate"])
        job = self.repo.conn.execute("SELECT * FROM jobs WHERE status='running'").fetchone()
        self.assertIsNotNone(job)
        self.assertEqual(job["attempts"], 1)
        self.assertGreater(job["lease_until"], self.clock())
        self.assertEqual(client.call.call_count, 1)
        client.fail_job.assert_not_called()
        model.assert_not_called()

    def test_completed_job_with_lost_ack_is_not_failed_or_replayed(self):
        client = self.client("complete_association", committed=True)
        model = Mock(side_effect=self.model)
        worker = MemoryWorker(client, model, lambda: "")
        with self.assertRaises(MemoryUnavailable):
            worker.run_once(["associate"])
        self.assertEqual(self.repo.job_counts(), {"done": 1, "pending": 1})
        self.assertEqual(model.call_count, 1)
        self.assertEqual(sum(call.args[0] == "complete_association" for call in client.call.call_args_list), 1)
        client.fail_job.assert_not_called()

    def test_uncommitted_completion_waits_for_fenced_lease_recovery(self):
        client = self.client("complete_association", committed=False)
        worker = MemoryWorker(client, self.model, lambda: "")
        with self.assertRaises(MemoryUnavailable):
            worker.run_once(["associate"])
        job = self.repo.conn.execute("SELECT * FROM jobs WHERE status='running'").fetchone()
        client.fail_job.assert_not_called()
        self.assertEqual(job["attempts"], 1)
        self.assertGreater(job["lease_until"], self.clock())
        self.clock.advance(421)
        recovered = self.service.claim_job(["associate"])
        self.assertEqual(recovered["job"]["id"], job["id"])
        self.assertEqual(recovered["job"]["attempts"], 2)
        self.assertEqual(recovered["job"]["lease_recoveries"], 1)
        self.assertEqual(self.service.complete_association(job["id"], [], expected_attempt=1)["status"], "ignored")
        self.assertEqual(self.repo.get_job(job["id"])["status"], "running")


if __name__ == "__main__":
    unittest.main()
