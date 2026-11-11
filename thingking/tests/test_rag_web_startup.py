"""RAG admin reconnects after backend startup without changing its target."""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]


class RagWebStartupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bines-rag-startup-")
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": directory.name,
            "BINES_GATEWAY_DIR": directory.name + "/gateway",
            "BINES_HEALTH_DIR": directory.name + "/health",
            "BINES_MODEL_USAGE_PATH": directory.name + "/usage.db",
            "BINES_WEBSITE_BRIDGE_SPOOL": directory.name + "/website",
            "BINES_ADMIN_TOKEN": "isolated-admin", "BINES_MEMORY_BACKEND": "auto",
        })
        env.start()
        self.addCleanup(env.stop)
        self.web = importlib.import_module("thingking.rag_web_server")

    def exercise_startup(self, host, backend):
        direct = Mock()
        direct.ping.return_value = False
        direct.stats.return_value = {"source": "direct-memory"}
        gateway = Mock()
        gateway.ping.return_value = False
        stop = threading.Event()
        server = Mock()
        results = []

        def requests():
            http = self.web.app.test_client()
            headers = {"Authorization": "Bearer isolated-admin"}
            results.append(http.get("/api/health", headers=headers).status_code)
            direct.ping.return_value = True
            results.append(http.get("/api/health", headers=headers).status_code)
            results.append(http.get("/api/stats", headers=headers).get_json())
            stop.set()

        server.handle_request.side_effect = requests
        with patch.dict(os.environ, {"BINES_MEMORY_BACKEND": backend}), \
                patch.object(self.web, "ZMQ_HOST", host), patch.object(self.web, "client", direct), \
                patch.object(self.web, "make_server", return_value=server), \
                patch("chatBot.memory_hub.GatewayMemoryClient", return_value=gateway) as fallback:
            self.web._run(stop)
            self.assertIs(self.web.client, direct)
            fallback.assert_not_called()
        server.server_close.assert_called_once()
        self.assertEqual(results[:2], [503, 200])
        self.assertEqual(results[2]["result"], {"source": "direct-memory"})

    def test_container_backend_recovers_without_permanent_gateway_fallback(self):
        self.exercise_startup("memory-server", "auto")

    def test_explicit_remote_loopback_backend_recovers_without_fallback(self):
        self.exercise_startup("127.0.0.1", "remote")

    def test_local_auto_without_memory_server_keeps_gateway_fallback(self):
        for backend in ("auto", "embedded"):
            with self.subTest(backend=backend):
                direct, gateway, server = Mock(), Mock(), Mock()
                direct.ping.return_value = False
                stop = threading.Event()
                stop.set()
                with patch.dict(os.environ, {"BINES_MEMORY_BACKEND": backend}), \
                        patch.object(self.web, "ZMQ_HOST", "127.0.0.1"), \
                        patch.object(self.web, "client", direct), patch.object(self.web, "make_server", return_value=server), \
                        patch("chatBot.memory_hub.GatewayMemoryClient", return_value=gateway) as fallback:
                    self.web._run(stop)
                    self.assertIs(self.web.client, gateway)
                    fallback.assert_called_once_with()

    def test_local_auto_ready_memory_server_remains_direct(self):
        direct, server = Mock(), Mock()
        direct.ping.return_value = True
        stop = threading.Event()
        stop.set()
        with patch.object(self.web, "ZMQ_HOST", "localhost"), \
                patch.object(self.web, "client", direct), patch.object(self.web, "make_server", return_value=server), \
                patch("chatBot.memory_hub.GatewayMemoryClient") as fallback:
            self.web._run(stop)
            self.assertIs(self.web.client, direct)
            fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
