import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import helpers
from common import healthcheck


class WorkerHeartbeatTest(unittest.TestCase):
    def test_requires_recent_live_and_healthy_worker(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BINES_HEALTH_DIR": folder}):
            self.assertFalse(healthcheck.check("memory-worker"))
            healthcheck.heartbeat("memory-worker")
            self.assertTrue(healthcheck.check("memory-worker"))
            with patch("common.healthcheck.os.kill", side_effect=ProcessLookupError):
                self.assertFalse(healthcheck.check("memory-worker"))
            path = healthcheck._marker("memory-worker")
            data = json.loads(path.read_text())
            data["updated"] = time.time() - 60
            path.write_text(json.dumps(data))
            self.assertFalse(healthcheck.check("memory-worker"))
            healthcheck.heartbeat("memory-worker", healthy=False)
            self.assertFalse(healthcheck.check("memory-worker"))
            path.write_text("{incomplete")
            self.assertFalse(healthcheck.check("memory-worker"))

    def test_heartbeat_io_failure_does_not_interrupt_worker(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BINES_HEALTH_DIR": folder}), patch(
            "common.healthcheck.Path.write_text", side_effect=OSError("temporary disk failure"),
        ), self.assertLogs("bines.health", level="WARNING"):
            self.assertFalse(healthcheck.heartbeat("memory-worker"))
            self.assertFalse(healthcheck.check("memory-worker"))


class BusinessProbeTest(unittest.TestCase):
    def probe(self, component, result):
        client = Mock(url="http://127.0.0.1:1", token="probe-test-token")
        client.opener.open.return_value = io.BytesIO(json.dumps({"result": result}).encode())
        with patch("chatBot.gateway_client.GatewayClient", return_value=client):
            healthy = healthcheck.check(component)
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 4)
        self.assertEqual(client.opener.open.call_args.args[0].get_header("Authorization"), "Bearer probe-test-token")
        return healthy

    def test_open_port_without_working_dependencies_is_unhealthy(self):
        for result in ({}, {"connected": True}, {"memory": True, "connected": False},
                       {"memory": True, "connected": True, "transport": {"event_consumer_alive": False}}):
            with self.subTest(result=result):
                self.assertFalse(self.probe("gateway", result))

    def test_qq_worker_requires_live_heartbeat_in_addition_to_gateway(self):
        ready = {"connected": True, "memory": True, "queue": {"workers_live": 0}}
        self.assertTrue(self.probe("gateway", ready))
        self.assertFalse(self.probe("qq-thinking", ready))
        ready["queue"]["workers_live"] = 1
        self.assertTrue(self.probe("qq-thinking", ready))
