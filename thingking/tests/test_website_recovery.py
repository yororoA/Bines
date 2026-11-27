import os
import json
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import helpers
import website_runtime as runtime
from common.website_spool import atomic_write_json, heartbeat, signed_envelope
from website_bridge import SpoolWorker


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.messages = []
        self.env = patch.dict(os.environ, {"BINES_WEBSITE_ENABLED": "true"})
        self.env.start()
        self.supervisor = runtime.WebsiteBridgeSupervisor(
            root=self.root, emit=self.messages.append, retry_seconds=.02,
            max_retry_seconds=.04, check_seconds=.02, stale_seconds=.1,
        )

    def tearDown(self):
        self.supervisor.close()
        self.env.stop()
        self.temp.cleanup()

    def pulse(self, token="token"):
        atomic_write_json(self.root / ".credentials.json", {"token": token})
        atomic_write_json(self.root / "heartbeat.json", heartbeat(token))

    def test_startup_failure_recovers_without_restart_of_launcher(self):
        recovered = threading.Event()
        bridge = Mock()
        bridge.process.poll.return_value = None
        attempts = []

        def start(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise RuntimeError("unavailable")
            self.pulse()
            recovered.set()

        bridge.start.side_effect = start
        with patch.object(runtime, "WebsiteBridgeProcess", return_value=bridge):
            self.assertTrue(self.supervisor.start())
            self.assertTrue(recovered.wait(2))
            self.supervisor.close()
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(attempt["managed"] for attempt in attempts))
        self.assertTrue(any("自动重试" in message for message in self.messages))

    def test_live_bridge_is_adopted_without_creating_a_process(self):
        self.pulse()
        with patch.object(runtime, "WebsiteBridgeProcess") as process:
            self.assertTrue(self.supervisor._tick())
            process.assert_not_called()
        self.assertTrue(self.supervisor.ready)

    def test_exited_owned_process_is_replaced_even_with_recent_heartbeat(self):
        self.pulse()
        old, new = Mock(), Mock()
        old.process.poll.return_value = 1
        new.process.poll.return_value = None
        new.start.side_effect = lambda **_: self.pulse("rotated")
        self.supervisor.bridge = old
        with patch.object(runtime, "WebsiteBridgeProcess", return_value=new):
            self.assertTrue(self.supervisor._tick())
        old.close.assert_called_once()
        new.start.assert_called_once_with(managed=True)

    def test_stale_or_unsigned_heartbeat_is_not_adopted(self):
        self.pulse()
        atomic_write_json(self.root / "heartbeat.json", signed_envelope(
            "token", "heartbeat", {"ts": time.time() - 60}))
        self.assertFalse(self.supervisor._healthy())
        self.pulse("wrong")
        atomic_write_json(self.root / ".credentials.json", {"token": "token"})
        self.assertFalse(self.supervisor._healthy())

    def test_second_supervisor_is_denied_but_cannot_stop_first(self):
        self.pulse()
        self.supervisor.check_seconds = 100
        self.supervisor.start()
        second = runtime.WebsiteBridgeSupervisor(root=self.root, emit=self.messages.append)
        try:
            with self.assertRaises(RuntimeError):
                second.start()
            second.close()
            self.assertTrue(self.supervisor.thread.is_alive())
        finally:
            second.close()

    def test_disabled_and_close_cancel_retries(self):
        with patch.dict(os.environ, {"BINES_WEBSITE_ENABLED": "false"}):
            self.assertFalse(self.supervisor.start())
        bridge = Mock()
        bridge.start.side_effect = RuntimeError("failed")
        self.supervisor.retry_seconds = 100
        with patch.object(runtime, "WebsiteBridgeProcess", return_value=bridge):
            self.supervisor.start()
            self.supervisor.close()
            attempts = bridge.start.call_count
            self.assertFalse(self.supervisor.thread.is_alive())
        self.assertLessEqual(attempts, 1)

    def test_reconnect_uses_live_spool_credentials_without_container_restart(self):
        from tools.website_tool import _spool_request

        class Service:
            def status(self, platform):
                return {"status": "ok", "platform": platform}

        with patch.dict(os.environ, {"BINES_WEBSITE_BRIDGE_TOKEN": "old-container-token"}):
            for token in ("first-live", "second-live"):
                worker = SpoolWorker(Service(), self.root, token)
                try:
                    worker.start()
                    self.assertTrue(self.supervisor._tick())
                    self.assertEqual(json.loads(_spool_request(self.root, "/v1/status",
                                                              {"platform": "bilibili"}, 2))["status"], "ok")
                finally:
                    worker.close()

    def test_real_child_startup_failure_and_exit_recover_via_spool(self):
        """Exercise actual subprocesses, locks and rotating credentials without Chrome."""
        from tools.website_tool import _spool_request
        root = self.root
        (root / "website_bridge.py").write_text(
            "import sys,argparse,signal\n"
            f"sys.path.insert(0,{str(runtime.ROOT)!r})\n"
            "from website_bridge import SpoolWorker,BridgeServer,BridgeHandler\n"
            "p=argparse.ArgumentParser()\n"
            "p.add_argument('--host');p.add_argument('--port',type=int)\n"
            "p.add_argument('--token');p.add_argument('--spool')\n"
            "a,_=p.parse_known_args()\n"
            "class Service:\n"
            " def status(self,platform):return {'status':'ok','platform':platform}\n"
            "s=Service();w=SpoolWorker(s,a.spool,a.token)\n"
            "h=BridgeServer((a.host,a.port),BridgeHandler,token=a.token,service=s)\n"
            "w.start()\n"
            "def stop(*_):raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            "try:h.serve_forever(poll_interval=.02)\n"
            "finally:w.close();h.server_close()\n"
        )
        ready = threading.Event()
        self.supervisor.emit = lambda message: ready.set() if "恢复" in message else None
        self.supervisor.stale_seconds = 5
        with patch.object(runtime, "ROOT", root), patch.object(runtime, "WEBSITE_SPOOL_ROOT", root), patch.object(
            runtime, "AGENT_BROWSER_SOCKET_DIR", root / "runtime"
        ), patch.object(runtime, "AGENT_BROWSER_RUNTIME_HOME", root / "home"), patch.object(
            runtime, "AGENT_BROWSER_CRASH_DIR", root / "crashes"
        ), patch.object(runtime, "ensure_agent_browser", side_effect=[RuntimeError("initial failure"), ["test"], ["test"]]), patch.object(
            runtime, "ensure_browser_node", return_value=sys.executable
        ), patch.object(runtime, "ensure_chrome_for_testing", return_value="/test/chrome"):
            self.supervisor.start()
            try:
                self.assertTrue(ready.wait(5))
                first = self.supervisor.bridge.process
                first_token = self.supervisor.bridge.token
                self.assertEqual(json.loads(_spool_request(root, "/v1/status", {}, 2))["status"], "ok")
                ready.clear()
                # Abrupt death leaves a heartbeat/credential behind; the OS
                # releases the worker lock and the supervisor rotates the token.
                first.kill()
                first.wait(timeout=2)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if self.supervisor.ready and self.supervisor.bridge.token != first_token:
                        break
                    ready.wait(.02)
                    ready.clear()
                self.assertNotEqual(self.supervisor.bridge.token, first_token)
                self.assertTrue(self.supervisor.ready)
                self.assertEqual(json.loads(_spool_request(root, "/v1/status", {}, 2))["status"], "ok")
            finally:
                self.supervisor.close()


if __name__ == "__main__":
    unittest.main()
