"""Isolated dashboard contract and degraded-service checks; no production data."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]

from flask import Flask
from common.admin_auth import install_admin_guard
from server import status_dashboard as dashboard


class StatusDashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bines-dashboard-")
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"BINES_ENV_FILE": "/dev/null",
            "BINES_MEMORY_DIR": self.directory.name, "BINES_AUTONOMY_BACKEND": "sqlite",
            "BINES_AUTONOMY_SQLITE_PATH": self.directory.name + "/autonomy.db",
            "BINES_ADMIN_TOKEN": "test-only-admin", "BINES_MEMORY_RPC_TOKEN": "test-only-memory",
            "BINES_QQ_GATEWAY_TOKEN": "test-only-gateway"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def app(self):
        app = Flask(__name__)
        install_admin_guard(app, "test-dashboard", entrypoints=("/status",), token_provider=lambda: "isolated-token")
        app.register_blueprint(dashboard.create_status_blueprint(None, {"MODULE_MANAGER": 5000, "RAG_MEMORY": 5001}))
        return app

    def test_authenticated_page_and_api_share_guard(self):
        client = self.app().test_client()
        self.assertEqual(client.get("/api/status/overview").status_code, 401)
        self.assertEqual(client.get("/status", headers={"Host": "attacker.invalid"}).status_code, 403)
        response = client.get("/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/status-assets/status_dashboard.js", response.data)
        self.assertIn(b"--target-dir", response.data)
        with client.get("/status-assets/status_dashboard.js") as static_response:
            self.assertEqual(static_response.status_code, 200)
        with patch.object(dashboard, "_memory", return_value={"jobs": {"pending": 2}}), \
                patch.object(dashboard, "_gateway", return_value={"connected": False}), \
                patch.object(dashboard, "_autonomy", return_value={"configured": False}):
            response = client.get("/api/status/overview")
        self.assertEqual(response.status_code, 200)
        components = response.get_json()["result"]["components"]
        self.assertEqual(components["memory"]["status"], "online")
        self.assertEqual(components["gateway"]["status"], "online")
        self.assertFalse(components["gateway"]["data"]["connected"])
        self.assertEqual(components["services"]["status"], "unknown")
        self.assertEqual(components["autonomy"]["status"], "unknown")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_failed_component_isolated_and_errors_do_not_expose_credentials(self):
        client = self.app().test_client()
        client.get("/status")
        with patch.object(dashboard, "_memory", side_effect=ConnectionError("secret-password")), \
                patch.object(dashboard, "_gateway", return_value={"connected": True}), \
                patch.object(dashboard, "_autonomy", side_effect=ValueError("redis://private:secret@host")):
            response = client.get("/api/status/overview")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["result"]["components"]
        self.assertEqual(data["memory"]["status"], "offline")
        self.assertEqual(data["gateway"]["status"], "online")
        self.assertEqual(data["autonomy"]["status"], "unknown")
        self.assertNotIn(b"secret", response.data)
        self.assertNotIn(b"redis://", response.data)

    def test_refreshes_coalesce_within_cache_window(self):
        client = self.app().test_client()
        client.get("/status")
        with patch.object(dashboard, "_memory", return_value={}) as memory, \
                patch.object(dashboard, "_gateway", return_value={}), \
                patch.object(dashboard, "_autonomy", return_value={}):
            first = client.get("/api/status/overview").get_json()
            second = client.get("/api/status/overview").get_json()
        self.assertEqual(first, second)
        memory.assert_called_once()

    def test_authentication_failure_is_unknown_not_offline(self):
        from urllib.error import HTTPError
        result = dashboard._probe(Mock(side_effect=HTTPError("private-host", 401, "secret", None, None)))
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("secret", json.dumps(result))

    def test_stalled_probe_has_deadline_and_is_not_resubmitted(self):
        release = threading.Event()
        def slow_memory():
            release.wait(3)
            return {}
        client = self.app().test_client()
        client.get("/status")
        try:
            with patch.object(dashboard, "_memory", side_effect=slow_memory) as memory, \
                    patch.object(dashboard, "_gateway", return_value={"connected": True}), \
                    patch.object(dashboard, "_autonomy", return_value={}), \
                    patch.object(dashboard, "PROBE_DEADLINE", .03), \
                    patch.object(dashboard, "CACHE_SECONDS", 0):
                started = time.monotonic()
                for _ in range(2):
                    data = client.get("/api/status/overview").json["result"]["components"]
                    self.assertEqual(data["memory"]["status"], "unknown")
                    self.assertEqual(data["gateway"]["status"], "online")
                self.assertLess(time.monotonic() - started, .5)
                memory.assert_called_once()
        finally:
            release.set()

    def test_autonomy_probe_does_not_create_or_mutate_sqlite(self):
        path = Path(self.directory.name) / "autonomy.db"
        self.assertEqual(dashboard._autonomy(), {"configured": False, "backend": "sqlite"})
        self.assertFalse(path.exists())
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE documents (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO documents VALUES(?,?)", ("control", json.dumps({"mode": "pause", "reason": "private", "updated_at": 5})))
        before = path.read_bytes()
        self.assertEqual(dashboard._autonomy(), {"configured": True, "backend": "sqlite", "control": "pause", "updated_at": 5})
        self.assertEqual(path.read_bytes(), before)

    def test_memory_uses_existing_life_and_drops_admin_metadata(self):
        client = Mock()
        client.call.side_effect = [{"memories": {"owner": {"semantic": 3}}, "life": {"energy": .8},
            "identity_links": [{"external_id": "private"}], "conversations": ["private"],
            "vector_status": {"ready": True, "last_error": "private-secret"}},
            {"data": {"mood": "calm"}, "version": 1}]
        with patch("memory.client.MemoryClient", return_value=client) as factory, \
                patch.dict(os.environ, {"BINES_MEMORY_BACKEND": "remote"}):
            data = dashboard._memory()
        self.assertEqual(data["life"], {"energy": .8})
        self.assertNotIn("identity_links", data)
        self.assertNotIn("conversations", data)
        self.assertNotIn("last_error", data["vector_status"])
        self.assertEqual(factory.call_args.kwargs["timeout_ms"], 1500)
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ["stats", "get_state"])

    def test_embedded_memory_uses_gateway_without_rpc(self):
        with patch("memory.client.MemoryClient") as memory, \
                patch.dict(os.environ, {"BINES_MEMORY_BACKEND": "embedded"}), \
                patch.object(dashboard, "_gateway_request", side_effect=[{"life": {}}, {"data": {}}]) as gateway:
            dashboard._memory()
        memory.return_value.call.assert_not_called()
        self.assertEqual([row.args[1]["method"] for row in gateway.call_args_list], ["stats", "get_state"])

    def test_state_failure_does_not_hide_memory_stats(self):
        client = Mock()
        client.call.side_effect = [{"jobs": {"pending": 3}}, TimeoutError("private")]
        with patch("memory.client.MemoryClient", return_value=client), \
                patch.dict(os.environ, {"BINES_MEMORY_BACKEND": "remote"}):
            data = dashboard._memory()
        self.assertEqual(data["jobs"]["pending"], 3)
        self.assertTrue(data["state_unavailable"])


if __name__ == "__main__":
    unittest.main()
