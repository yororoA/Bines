"""Real domain round trips against isolated temporary stores; no live accounts."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src"), str(ROOT / "thingking" / "tests")]

from flask import Flask
from common.admin_auth import install_admin_guard
from server import state_controls as controls


class StateControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bines-controls-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(self.root),
            "BINES_GATEWAY_DIR": str(self.root / "gateway"), "BINES_HEALTH_DIR": str(self.root / "health"),
            "BINES_MODEL_USAGE_PATH": str(self.root / "usage.db"), "BINES_WEBSITE_BRIDGE_SPOOL": str(self.root / "spool"),
            "BINES_AUTONOMY_BACKEND": "sqlite", "BINES_AUTONOMY_SQLITE_PATH": str(self.root / "autonomy.db"),
            "BINES_ADMIN_TOKEN": "test-admin", "BINES_MEMORY_RPC_TOKEN": "test-memory",
            "BINES_QQ_GATEWAY_TOKEN": "test-gateway", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)
        from helpers import Clock
        from memory.client import MemoryClient
        from memory.lexical_index import LexicalIndex
        from memory.repository import MemoryRepository
        from memory.server import dispatch
        from memory.service import MemoryService
        from memory.settings import MemorySettings
        self.clock = Clock()
        self.repo = MemoryRepository(self.root / "memory.db", self.clock)
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=self.root, state_updates=False, associations_enabled=False), self.clock)
        service = self.service

        class DispatchClient(MemoryClient):
            def __init__(self):
                pass

            def call(self, method, timeout_ms=None, **params):
                response = dispatch(service, {"method": method, "params": params, "token": "test-memory"},
                                    token="test-memory")
                if not response["ok"]:
                    raise RuntimeError(response["error"])
                return response["result"]

        self.memory = DispatchClient()
        self.real_factory = controls._memory_client
        client_patch = patch.object(controls, "_memory_client", return_value=self.memory)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.app = Flask(__name__)
        self.app.register_blueprint(controls.create_state_controls_blueprint())
        self.app.add_url_rule("/control", view_func=lambda: "control")
        install_admin_guard(self.app, "test-control", entrypoints=("/control",), token_provider=lambda: "test-admin")
        self.client = self.app.test_client()
        self.client.get("/control")

    def test_auth_and_origin_guard_every_mutation(self):
        client = self.app.test_client()
        for path in ("/api/control/autonomy", "/api/control/life", "/api/control/jobs/1/retry", "/api/control/index/rebuild"):
            self.assertEqual(client.post(path, json={}).status_code, 401)
        self.assertEqual(client.put("/api/control/state", json={}).status_code, 401)
        self.assertEqual(client.get("/api/control/runtime").status_code, 401)
        self.assertEqual(client.get("/api/control/schedulers").status_code, 401)
        self.assertEqual(self.client.post("/api/control/autonomy", json={"mode": "pause"},
            headers={"Origin": "https://external.invalid"}).status_code, 403)

    def test_state_round_trip_all_scopes_and_stale_version_never_overwrites(self):
        scope = "qq:test:group:1:user:synthetic"
        saved = self.client.put("/api/control/state", json={"scope": scope, "expected_version": 0,
            "changes": {"current_location": "合成书房", "add_item": "合成书", "relationship_delta": 2}})
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(saved.json["result"]["data"]["current_location"], "合成书房")
        self.assertEqual(saved.json["result"]["version"], 1)
        runtime = self.client.get("/api/control/runtime", query_string={"scope": scope}).json["result"]
        self.assertIn(scope, runtime["scopes"])
        self.assertEqual(runtime["state"]["data"]["data"]["inventory"], ["合成书"])
        self.assertFalse(runtime["capabilities"]["host_services"])
        self.assertFalse(runtime["capabilities"]["life"])
        self.assertFalse(runtime["host"]["available"])
        # A state-only scope must appear despite having no memories/messages.
        self.assertIn(scope, self.service.stats()["state_scopes"])
        stale = self.client.put("/api/control/state", json={"scope": scope, "expected_version": 0,
            "changes": {"current_location": "错误覆盖"}})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.service.get_state(scope)["data"]["current_location"], "合成书房")

    def test_legacy_extensions_preserve_semantic_state_and_version(self):
        scope = "legacy:fixture"
        self.service.put_state(scope, {"mood": "高兴", "inventory": ["钢笔"], "relationship_score": 4})
        state = self.client.get("/api/control/runtime", query_string={"scope": scope}).json["result"]["state"]["data"]
        self.assertEqual(state["extensions"], {"mood": "高兴"})
        body = {"scope": scope, "expected_version": state["version"], "extensions": {"mood": "期待"}}
        route = "/api/control/state/extensions"
        self.assertEqual(self.client.put(route, json={**body, "extensions": {"mood": 123}}).status_code, 400)
        self.assertEqual(self.client.put(route, json={**body, "extensions": {"relationship_score": 500}}).status_code, 400)
        self.assertEqual(self.client.put(route, json=body).status_code, 200)
        self.assertEqual(self.client.put(route, json=body).status_code, 409)
        self.assertEqual(self.service.get_state(scope)["data"], {"mood": "期待", "inventory": ["钢笔"], "relationship_score": 4})

    def test_life_regulation_persists_and_deduplicates_same_operation(self):
        body = {"scope": "owner", "action": "rest", "duration_minutes": 15,
                "reason": "界面合成测试", "operation_id": "synthetic-rest-01"}
        first = self.client.post("/api/control/life", json=body)
        self.assertEqual(first.status_code, 200, first.json)
        life = self.service.life_snapshot()
        self.assertEqual(life["regulation"]["mode"], "rest")
        events = self.service.life.event_count()
        second = self.client.post("/api/control/life", json=body)
        self.assertEqual(second.status_code, 200, second.json)
        self.assertEqual(second.json["result"]["status"], "duplicate")
        self.assertEqual(self.service.life.event_count(), events)
        restored = self.client.post("/api/control/life", json={**body, "action": "auto", "duration_minutes": 0,
            "operation_id": "synthetic-auto-01"})
        self.assertEqual(restored.status_code, 200, restored.json)
        self.assertFalse(self.service.life_snapshot()["regulation"])

    def test_invalid_actions_and_types_are_rejected_before_domain_calls(self):
        invalid = [
            ("put", "/api/control/state", {"scope": "owner", "expected_version": 0, "changes": {"energy": .8}}),
            ("put", "/api/control/state", {"scope": "owner", "expected_version": True, "changes": {"current_time": "now"}}),
            ("put", "/api/control/state", {"scope": "owner", "expected_version": 0, "changes": {"add_item": []}}),
            ("post", "/api/control/life", {"action": "delete", "reason": "测试", "operation_id": "synthetic-01"}),
            ("post", "/api/control/life", {"action": [], "reason": "测试", "operation_id": "synthetic-01"}),
            ("post", "/api/control/autonomy", {"mode": "execute"}),
            ("post", "/api/control/autonomy", {"mode": []}),
        ]
        for method, path, body in invalid:
            with self.subTest(path=path, body=body):
                response = getattr(self.client, method)(path, json=body)
                self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.service.get_state()["version"], 0)

    def test_autonomy_read_is_nonseeding_and_write_uses_real_store(self):
        database = self.root / "autonomy.db"
        legacy = self.root / "autonomy_control.json"
        legacy.write_text(json.dumps({"mode": "stop", "reason": "synthetic legacy"}))
        self.assertFalse(controls._autonomy_control()["configured"])
        self.assertFalse(database.exists())
        for mode in ("pause", "resume", "stop"):
            response = self.client.post("/api/control/autonomy", json={"mode": mode, "reason": "合成管理操作"})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(controls._autonomy_control()["mode"], mode)
        before = database.read_bytes()
        control = controls._autonomy_control()
        self.assertGreater(control["revision"], 0)
        self.assertEqual(before, database.read_bytes())
        self.assertEqual(json.loads(legacy.read_text())["mode"], "stop")

    def test_scheduler_pages_use_qq_storage_and_exclude_private_turn_data(self):
        from common.autonomy_store import AutonomyStore
        gateway = self.root / "gateway"
        endpoint = "/api/control/schedulers"
        with patch("chatBot.config.GATEWAY_DIR", gateway), patch.object(controls, "_SCHEDULER_PAGE", 2):
            self.assertEqual(self.client.get(endpoint).json["result"], {"items": [], "next_cursor": None})
            self.assertFalse(gateway.exists())
            self.assertFalse((self.root / "autonomy.db").exists())
            desktop = AutonomyStore(sqlite_path=self.root / "autonomy.db")
            qq = AutonomyStore(sqlite_path=gateway / "gateway.autonomy.db")
            document = {"activity": {"mode": "review", "intent": "private model wording"},
                        "pending": {"kind": "initiative", "topic": "private interests"},
                        "waiting_activities": {"private key": {"observations": ["private"]}},
                        "intention_queue": [{"reason": "private"}],
                        "next_decision": 123, "drives": {"initiative": 0.51, "private": "omit me"},
                        "emotion": {"valence": 0.25}, "user_ref": "private-owner"}
            desktop.put("desktop", document, 0)
            for index in range(3):
                qq.put(f"qq:private-account-{index}", document, 0)
            qq.put("control", {"reason": "private"}, 0)
            page = self.client.get(endpoint)
            self.assertEqual(page.status_code, 200)
            self.assertNotIn("private", page.get_data(as_text=True))
            first = page.json["result"]
            self.assertEqual(first["next_cursor"], "2")
            self.assertEqual(len(first["items"]), 3)
            row = first["items"][0]
            self.assertEqual(row["channel"], "desktop")
            self.assertEqual(row["activity_mode"], "review")
            self.assertEqual(row["pending_kind"], "initiative")
            self.assertEqual(row["intention_count"], row["waiting_count"])
            self.assertEqual(row["intention_count"], 1)
            self.assertEqual(row["drives"], {"initiative": 0.51})
            self.assertEqual(row["emotion"], {"valence": 0.25})
            second = self.client.get(endpoint + "?cursor=2").json["result"]
            self.assertIsNone(second["next_cursor"])
            self.assertEqual(len(second["items"]), 1)
            self.assertNotIn(second["items"][0]["id"], {item["id"] for item in first["items"]})
            self.assertEqual(qq.get("qq:private-account-2"), (document, 1))
            self.assertEqual(self.client.get(endpoint + "?cursor=-1").status_code, 400)

    def failed_job(self, *, terminal=False, with_causes=True):
        payload = {"last_failure": {"causes": ["APIConnectionError"]}} if with_causes else {}
        if terminal:
            payload["terminal_reason"] = "lease_recovery_budget"
        jid = self.repo.enqueue_job("diary", "owner", payload, "synthetic-job")
        self.repo.conn.execute("UPDATE jobs SET status='failed',attempts=3,last_error=? WHERE id=?",
                              ("OtherError: secret-password https://private.invalid", jid))
        return jid

    def test_job_metadata_retry_and_stale_duplicate_are_safe(self):
        jid = self.failed_job()
        listed = self.client.get("/api/control/jobs?status=failed&limit=5").json["result"]
        job = listed["items"][0]
        self.assertEqual(job["error_type"], "APIConnectionError")
        self.assertIsNone(job["terminal_reason"])
        self.assertTrue(job["retryable"])
        self.assertNotIn("secret-password", json.dumps(listed))
        body = {"scope": job["scope"], "kind": job["kind"], "expected_attempt": job["attempts"],
            "expected_updated_at": job["updated_at"], "expected_error_type": job["error_type"],
            "reason": "connectivity_rechecked"}
        response = self.client.post(f"/api/control/jobs/{jid}/retry", json=body)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.repo.get_job(jid)["status"], "pending")
        self.assertEqual(self.repo.get_job(jid)["attempts"], 3)
        duplicate = self.client.post(f"/api/control/jobs/{jid}/retry", json=body)
        self.assertEqual(duplicate.status_code, 409, duplicate.json)
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_manual_retries").fetchone()[0], 1)

    def test_terminal_job_and_legacy_error_type_metadata(self):
        jid = self.failed_job(terminal=True, with_causes=False)
        job = self.client.get("/api/control/jobs").json["result"]["items"][0]
        self.assertEqual(job["error_type"], "OtherError")
        self.assertEqual(job["terminal_reason"], "lease_recovery_budget")
        self.assertFalse(job["retryable"])
        body = {"scope": job["scope"], "kind": job["kind"], "expected_attempt": job["attempts"],
            "expected_updated_at": job["updated_at"], "expected_error_type": job["error_type"],
            "reason": "configuration_corrected"}
        self.assertEqual(self.client.post(f"/api/control/jobs/{jid}/retry", json=body).status_code, 409)
        self.assertEqual(self.repo.get_job(jid)["status"], "failed")

    def test_backend_failure_isolated_and_secret_safe(self):
        with patch.object(controls, "_memory_client", side_effect=RuntimeError("redis://private:secret@host")):
            response = self.client.get("/api/control/runtime")
            self.assertFalse(response.json["result"]["state"]["available"])
            self.assertTrue(response.json["result"]["autonomy"]["available"])
            self.assertNotIn(b"private", response.data)
            write = self.client.put("/api/control/state", json={"scope": "owner", "expected_version": 0,
                "changes": {"current_location": "合成"}})
            self.assertEqual(write.status_code, 503)
            self.assertNotIn(b"secret", write.data)
        with patch("common.activity.set_owner_control", side_effect=RuntimeError("secret")):
            response = self.client.post("/api/control/autonomy", json={"mode": "pause"})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b"secret", response.data)

    def test_index_rebuild_uses_domain_interface(self):
        response = self.client.post("/api/control/index/rebuild", json={})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("total", response.json["result"])

    def test_embedded_transport_and_local_auto_fallback_do_not_replay_writes(self):
        from memory.client import MemoryUnavailable
        with patch.dict(os.environ, {"BINES_MEMORY_BACKEND": "embedded"}), \
                patch.object(controls, "_gateway_call", return_value={"version": 1}) as gateway:
            client = self.real_factory()
            self.assertEqual(client.put_state("owner", {}, expected_version=0,
                changes={"current_location": "合成"}), {"version": 1})
            self.assertEqual(gateway.call_args.args[0], "put_state")
        from memory.client import MemoryClient
        with patch.dict(os.environ, {"BINES_MEMORY_BACKEND": "auto"}), \
                patch("config.ZMQ_HOST", "127.0.0.1"), \
                patch.object(MemoryClient, "call", side_effect=MemoryUnavailable("synthetic unavailable")) as rpc, \
                patch.object(controls, "_gateway_call", return_value={"version": 1}) as gateway:
            client = self.real_factory()
            client.put_state("owner", {}, expected_version=0, changes={"current_time": "now"})
            self.assertEqual(rpc.call_args.args, ("ping",))
            self.assertEqual(gateway.call_args.args[0], "put_state")
        with patch.dict(os.environ, {"BINES_MEMORY_BACKEND": "auto"}), \
                patch("config.ZMQ_HOST", "127.0.0.1"), \
                patch.object(MemoryClient, "call", side_effect=[True, MemoryUnavailable("write result unknown")]), \
                patch.object(controls, "_gateway_call") as gateway:
            client = self.real_factory()
            with self.assertRaises(MemoryUnavailable):
                client.put_state("owner", {}, expected_version=0, changes={"current_time": "now"})
            gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
