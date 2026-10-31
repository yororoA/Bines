import concurrent.futures
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import helpers
from flask import Flask

from common.admin_auth import install_admin_guard
from common.local_auth import memory_token
from memory.client import MemoryClient
from memory.server import dispatch, serve


class CredentialsTest(unittest.TestCase):
    def test_concurrent_services_share_one_private_credential(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "BINES_MEMORY_DIR": folder, "BINES_MEMORY_RPC_TOKEN": "",
        }):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                tokens = list(pool.map(lambda _: memory_token(), range(16)))
            self.assertEqual(len(set(tokens)), 1)
            self.assertGreaterEqual(len(tokens[0]), 32)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE((Path(folder) / "memory-rpc.token").stat().st_mode), 0o600)

    def test_explicit_token_does_not_create_local_files(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "BINES_MEMORY_DIR": folder, "BINES_MEMORY_RPC_TOKEN": "configured-test-token",
        }):
            self.assertEqual(memory_token(), "configured-test-token")
            self.assertEqual(list(Path(folder).iterdir()), [])


class MemoryRpcSecurityTest(unittest.TestCase):
    def test_unauthorized_and_malformed_requests_never_reach_service(self):
        class Service:
            calls = 0
            def delete_memory(self, memory_id):
                self.calls += 1
                return {"deleted": memory_id}
        service = Service()
        for request in ([], None, {"method": "delete_memory", "params": {"memory_id": "private"}},
                        {"method": "delete_memory", "params": {}, "token": "wrong"},
                        {"method": "delete_memory", "params": {}, "token": "中文"},
                        {"method": "delete_memory", "params": {}, "token": "\ud800"},
                        {"method": "delete_memory", "params": [], "token": "test"},
                        {"method": [], "params": {}, "token": "test"}):
            with self.subTest(request_type=type(request).__name__):
                self.assertFalse(dispatch(service, request, token="test")["ok"])
        self.assertEqual(service.calls, 0)
        result = dispatch(service, {"method": "delete_memory", "params": {"memory_id": "private"},
                                    "token": "test"}, token="test")
        self.assertTrue(result["ok"])
        self.assertEqual(service.calls, 1)

    def test_real_rpc_requires_shared_credential_and_survives_bad_requests(self):
        import zmq
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        closed = threading.Event()
        class Repo:
            def close(self):
                closed.set()
        class Service:
            repo = Repo()
            def ping(self):
                return {"ok": True}
        ready, stop = threading.Event(), threading.Event()
        with patch("memory.server.build_service", return_value=Service()):
            thread = threading.Thread(target=serve, args=(port, ready.set), kwargs={
                "host": "127.0.0.1", "token": "private-test-token", "stop_event": stop,
            })
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                self.assertFalse(MemoryClient("127.0.0.1", port, token="wrong").ping())
                with zmq.Context() as context, context.socket(zmq.REQ) as raw:
                    raw.setsockopt(zmq.RCVTIMEO, 2000)
                    raw.connect(f"tcp://127.0.0.1:{port}")
                    raw.send_json(["malformed"])
                    self.assertFalse(raw.recv_json()["ok"])
                    for bad_token in ("中文", "\ud800", {"token": "wrong"}):
                        raw.send_json({"method": "ping", "token": bad_token})
                        self.assertFalse(raw.recv_json()["ok"])
                    raw.send(b"[" * 5000)
                    self.assertFalse(raw.recv_json()["ok"])
                    raw.send_multipart([b"{}", b"unexpected"])
                    self.assertFalse(raw.recv_json()["ok"])
                self.assertTrue(MemoryClient("127.0.0.1", port, token="private-test-token").ping())
            finally:
                stop.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(closed.is_set())


class AdminSecurityTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"BINES_ADMIN_ALLOWED_HOSTS": "", "BINES_ADMIN_LOCAL_PROXY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = Flask(__name__)
        self.app.add_url_rule("/", "home", lambda: "management")
        self.app.add_url_rule("/api/change", "change", lambda: {"ok": True}, methods=["GET", "POST"])
        install_admin_guard(self.app, "test", token_provider=lambda: "private-admin-test-token")
        self.http = self.app.test_client()

    def test_local_page_bootstraps_session_but_bare_api_is_denied(self):
        self.assertEqual(self.http.post("/api/change", headers={"Authorization": "Bearer café"}).status_code, 401)
        self.assertEqual(self.http.post("/api/change", json={}).status_code, 401)
        page = self.http.get("/")
        self.assertEqual(page.status_code, 200)
        cookie = page.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("private-admin-test-token", cookie)
        self.assertEqual(self.http.post("/api/change", json={}).status_code, 200)
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", page.headers)

    def test_rebinding_cross_origin_and_cross_site_are_rejected_even_with_session(self):
        self.http.get("/")
        cases = [({"Host": "attacker.example"}, "/"),
                 ({"Origin": "https://attacker.example"}, "/api/change"),
                 ({"Origin": "http://localhost:9999"}, "/api/change"),
                 ({"Origin": "null"}, "/api/change"),
                 ({"Sec-Fetch-Site": "cross-site"}, "/")]
        for headers, path in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.http.get(path, headers=headers).status_code, 403)

    def test_remote_client_cannot_bootstrap_or_forge_forwarded_address(self):
        result = self.http.get("/", environ_overrides={"REMOTE_ADDR": "192.0.2.4"},
                               headers={"X-Forwarded-For": "127.0.0.1"})
        self.assertEqual(result.status_code, 401)
        self.assertNotIn("Set-Cookie", result.headers)

    def test_explicit_remote_host_and_bearer_token(self):
        with patch.dict(os.environ, {"BINES_ADMIN_ALLOWED_HOSTS": "admin.example"}):
            headers = {"Host": "admin.example", "Authorization": "Bearer private-admin-test-token"}
            self.assertEqual(self.http.post("/api/change", headers=headers,
                             environ_overrides={"REMOTE_ADDR": "192.0.2.4"}).status_code, 200)
            headers["Authorization"] = "Bearer wrong"
            self.assertEqual(self.http.post("/api/change", headers=headers).status_code, 401)

    def test_expired_or_forged_session_is_denied(self):
        import time
        from itsdangerous import URLSafeTimedSerializer
        serializer = URLSafeTimedSerializer("private-admin-test-token", salt="bines-admin:test")
        old = time.time() - 86410
        with patch("itsdangerous.timed.time.time", return_value=old):
            expired = serializer.dumps({"admin": True})
        for cookie in (expired, "forged-session"):
            self.http.set_cookie("bines_test_session", cookie)
            self.assertEqual(self.http.get("/api/change").status_code, 401)

    def test_docker_local_forwarding_trusts_only_gateway_and_loopback_host(self):
        route = "Iface Destination Gateway Flags\neth0 00000000 010012AC 0003\n"
        with patch.dict(os.environ, {"BINES_ADMIN_LOCAL_PROXY": "docker"}), patch(
            "common.admin_auth.Path.read_text", return_value=route,
        ):
            self.assertEqual(self.http.get("/", environ_overrides={"REMOTE_ADDR": "172.18.0.2"}).status_code, 401)
            self.assertEqual(self.http.get("/", environ_overrides={"REMOTE_ADDR": "172.18.0.1"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
