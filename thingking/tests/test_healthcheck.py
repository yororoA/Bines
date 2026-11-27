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

    def test_login_wait_is_ready_but_not_business_healthy(self):
        waiting = {"memory": True, "connected": False,
                   "transport": {"event_consumer_alive": True}, "queue": {"workers_live": 1}}
        for component in ("gateway", "qq-thinking"):
            self.assertFalse(self.probe(component, waiting))
            self.assertTrue(self.probe(component + "-ready", waiting))
        self.assertIs(waiting["connected"], False)

    def test_readiness_requires_live_local_dependencies(self):
        waiting = {"memory": True, "connected": False, "transport": {"event_consumer_alive": True},
                   "queue": {"workers_live": 1}}
        for patch_result in ({"memory": False}, {"transport": {}}, {"transport": None},
                             {"transport": {"event_consumer_alive": False}}):
            for component in ("gateway-ready", "qq-thinking-ready"):
                with self.subTest(component=component, result=patch_result):
                    self.assertFalse(self.probe(component, {**waiting, **patch_result}))
        for queue in ({}, {"workers_live": 0}, {"workers_live": "1"}, None):
            self.assertTrue(self.probe("gateway-ready", {**waiting, "queue": queue}))
            self.assertFalse(self.probe("qq-thinking-ready", {**waiting, "queue": queue}))

    def test_compose_uses_readiness_without_changing_business_probe_names(self):
        from pathlib import Path
        import yaml
        compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "deploy/docker/compose.yaml").read_text())
        self.assertEqual(compose["services"]["chatbot"]["healthcheck"]["test"][-1], "gateway-ready")
        self.assertEqual(compose["services"]["qq-thinking"]["healthcheck"]["test"][-1], "qq-thinking-ready")
        for service in ("qq-thinking", "memory-worker"):
            self.assertEqual(compose["services"][service]["depends_on"]["chatbot"]["condition"], "service_healthy")
