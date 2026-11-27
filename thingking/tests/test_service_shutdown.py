"""Real process signals against fake services; no Docker, QQ, RPC or models."""
import importlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]


def fixture(kind, directory):
    root = Path(directory)

    def record(value):
        with (root / "events").open("a") as output:
            output.write(value + "\n")

    def ready(*_args, **_kwargs):
        (root / "ready").touch()

    if kind == "gateway":
        import chatBot.main as gateway
        end = threading.Event()
        server = Mock(server_port=6102)
        server.serve_forever.side_effect = end.wait
        server.shutdown.side_effect = lambda: (record("http-stop"), end.set())
        server.server_close.side_effect = lambda: record("http-close")
        transport = Mock()
        transport.disconnect.side_effect = lambda: record("napcat-close")
        memory = Mock()
        memory.close.side_effect = lambda: record("memory-close")
        with patch.object(gateway, "MemoryStore"), patch.object(gateway, "NapCatClient", return_value=transport), \
                patch.object(gateway, "MemoryHub", return_value=memory), patch.object(gateway, "CapabilityService"), \
                patch.object(gateway, "GatewayServer", return_value=server), patch("common.service_ready.mark_ready", ready):
            gateway.main(force_exit=True)
    elif kind == "qq":
        import thingking.qq_thinking as qq
        original = qq.QQThinking

        def construct(*, stop_event):
            bot = original.__new__(original)
            bot.stop, bot.memory, bot.store, bot.client = stop_event, Mock(), Mock(), Mock()

            def consume():
                ready()
                stop_event.wait()
                record("consumer-close")

            bot.worker = consume
            return bot

        with patch.object(qq, "QQThinking", construct), patch.object(qq, "WORKERS", 4), \
                patch("chatBot.proactive.enabled", return_value=False), patch("tools.dependencies.deps.register_memory_client"):
            qq.main(force_exit=True)
    elif kind == "memory":
        import thingking.rag_server as memory
        import memory.server as rpc
        socket = Mock()
        socket.poll.side_effect = lambda _timeout: threading.Event().wait(.02)
        socket.close.side_effect = lambda **_: record("socket-close")
        zmq = SimpleNamespace(REP=1, MAXMSGSIZE=2, RCVMORE=3,
                              Context=SimpleNamespace(instance=lambda: SimpleNamespace(socket=lambda *_: socket)))
        repo = SimpleNamespace(close=lambda: record("repository-close"))
        with patch.object(rpc, "build_service", return_value=SimpleNamespace(repo=repo)), \
                patch.object(memory, "_notify_ready", ready), patch.dict(sys.modules, {"zmq": zmq}):
            memory.main(force_exit=True)
    elif kind == "web":
        import thingking.rag_web_server as web
        server = Mock()
        server.handle_request.side_effect = lambda: threading.Event().wait(.02)
        server.server_close.side_effect = lambda: record("web-close")

        def make_server(*_args, **_kwargs):
            ready()
            return server

        with patch.object(web, "client", Mock()), patch.object(web, "make_server", make_server):
            web.main(force_exit=True)
    elif kind in {"blocked-rpc", "executor"}:
        from common.service_shutdown import run_service

        def run(stop):
            if kind == "blocked-rpc":
                ready()
                threading.Event().wait(60)
            else:
                from concurrent.futures import ThreadPoolExecutor
                pool = ThreadPoolExecutor(max_workers=1)
                pool.submit(threading.Event().wait, 60)
                ready()
                stop.wait()
                pool.shutdown(wait=False)
                record("executor-stop-requested")

        run_service(run, name=kind, force_exit=True)
    elif kind == "startup-error":
        from common.service_shutdown import run_service

        def fail(_stop):
            raise RuntimeError("fixture startup failed")

        run_service(fail, name=kind, force_exit=True)
    else:
        raise ValueError(kind)


class ServiceShutdownTests(unittest.TestCase):
    def run_fixture(self, kind, sig=None):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": directory,
                   "BINES_GATEWAY_DIR": directory, "BINES_WEBSITE_AUTO_OBSERVE": "false",
                   "_BINES_RUNTIME_DIR": directory, "_BINES_SERVICE_TOKEN": "fixture",
                   "_BINES_SERVICE_NAME": "fixture", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--fixture", kind, directory],
                                       cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if sig is not None:
                    deadline = time.monotonic() + 20
                    while not (Path(directory) / "ready").exists() and process.poll() is None:
                        if time.monotonic() >= deadline:
                            self.fail(f"{kind} did not reach fake-service readiness")
                        time.sleep(.02)
                    if process.poll() is not None:
                        self.fail(f"{kind} exited before signal: {process.communicate()}")
                    started = time.monotonic()
                    process.send_signal(sig)
                else:
                    started = time.monotonic()
                output, error = process.communicate(timeout=10)
                elapsed = time.monotonic() - started
                events = Path(directory) / "events"
                return process.returncode, elapsed, output + error, events.read_text().splitlines() if events.exists() else []
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    @unittest.skipIf(os.name == "nt", "Windows TerminateProcess does not deliver POSIX signals")
    def test_term_and_interrupt_cleanup_each_service_and_exit_successfully(self):
        expected = {"gateway": ["http-stop", "http-close", "napcat-close", "memory-close"],
                    "qq": ["consumer-close"] * 4, "memory": ["socket-close", "repository-close"], "web": ["web-close"]}
        for kind, events in expected.items():
            for sig in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(service=kind, signal=sig):
                    code, elapsed, output, actual = self.run_fixture(kind, sig)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(actual, events, output)
                    self.assertLess(elapsed, 3, output)

    @unittest.skipIf(os.name == "nt", "Windows TerminateProcess does not deliver POSIX signals")
    def test_long_rpc_and_executor_atexit_cannot_exceed_shutdown_deadline(self):
        for kind, sig, expected_code in (("blocked-rpc", signal.SIGTERM, 143),
                                          ("executor", signal.SIGINT, 130)):
            with self.subTest(kind=kind):
                code, elapsed, output, events = self.run_fixture(kind, sig)
                self.assertEqual(code, expected_code, output)
                self.assertGreaterEqual(elapsed, 7.5)
                self.assertLess(elapsed, 10)
                self.assertIn("shutdown deadline exceeded after 8.0s", output)
                if kind == "executor":
                    self.assertEqual(events, ["executor-stop-requested"])

    def test_startup_failure_is_not_swallowed_as_success(self):
        code, _, output, _ = self.run_fixture("startup-error")
        self.assertNotEqual(code, 0)
        self.assertIn("fixture startup failed", output)

    def test_only_successful_health_access_logging_is_suppressed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": directory}), \
                patch("memory.client.MemoryClient.ping") as ping:
            web = importlib.import_module("thingking.rag_web_server")
            ping.assert_not_called()
        handler = web.QuietHealthRequestHandler.__new__(web.QuietHealthRequestHandler)
        with patch.object(web.WSGIRequestHandler, "log_request") as access:
            for command, path, code, visible in (
                ("GET", "/api/health", 200, False), ("GET", "/api/health", 503, True),
                ("GET", "/api/health", 401, True), ("GET", "/api/memories", 200, True),
                ("POST", "/api/health", 200, True),
            ):
                with self.subTest(command=command, path=path, code=code):
                    access.reset_mock()
                    handler.command, handler.path = command, path
                    handler.log_request(code, 10)
                    self.assertEqual(access.called, visible)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fixture":
        fixture(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
