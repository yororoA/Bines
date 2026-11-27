"""Behavioral autonomy storage and gate tests, isolated from live accounts."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from common.autonomy_store import AutonomyStore, AutonomyStoreError, StateConflict
from common.activity import activity_control, set_owner_control
from common.proactive import ProactiveConfig, ProactivePolicy
from chatBot.proactive import QQProactive
from chatBot.store import MemoryStore


class AutonomyDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bines-autonomy-tests-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "BINES_MEMORY_DIR": self.temp.name,
            "BINES_AUTONOMY_BACKEND": os.getenv("BINES_TEST_AUTONOMY_BACKEND", "sqlite"),
            "BINES_AUTONOMY_PREFIX": "bines:autonomy:test:" + self.directory.name + ":",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = MemoryStore(self.directory / "gateway.db", owners={"1"})
        self.event = {
            "post_type": "message", "message_type": "private", "self_id": 99,
            "user_id": 1, "message_id": 1, "time": time.time() - 3600,
            "sender": {"user_id": 1}, "message": [{"type": "text", "data": {"text": "test"}}],
        }
        self.store.ingest(self.event)
        with self.store.connection() as db:
            db.execute("UPDATE events SET status='done'")
        self.conversation = self.store.identity(self.event).conversation
        self.memory = Mock()
        self.memory.activity_snapshot.return_value = {"life": {"phase": "active"}}
        self.memory.build_context.return_value = {"life": {"phase": "active"}}
        self.client = Mock()
        self.client.request.return_value = {"connected": True}
        self.config = ProactiveConfig(startup_grace=0, quiet_start=0, quiet_end=0)

    def scheduler(self):
        return QQProactive(self.store, self.memory, self.client, config=self.config,
                           decide=Mock(return_value=None), appraise=Mock(return_value=None))

    def queue(self, kind, status="pending"):
        event = {**self.event, "post_type": kind}
        event_id = f"{kind}:{status}"
        with self.store.connection() as db:
            db.execute("INSERT INTO events(id,conversation,raw,received,status,lease) VALUES(?,?,?,?,?,?)",
                       (event_id, self.conversation, json.dumps(event), time.time() - 45, status,
                        time.time() + 60 if status == "working" else 0))

    def test_distinct_gates_include_running_queued_and_inbound_work(self):
        for kind, status, reason, label in (
            ("bines_proactive", "working", "autonomous_running", "自主任务执行中"),
            ("bines_proactive", "pending", "autonomous_queued", "自主任务待执行"),
            ("message", "pending", "user_messages_pending", "用户消息待处理"),
            ("message", "working", "user_messages_pending", "用户消息待处理"),
        ):
            with self.subTest(kind=kind, status=status):
                with self.store.connection() as db:
                    db.execute("DELETE FROM events WHERE status IN ('pending','working')")
                self.queue(kind, status)
                scheduler = self.scheduler()
                with self.assertLogs("bines.qq.proactive") as logs:
                    scheduler.tick()
                self.assertEqual(scheduler.policies[self.conversation].state.last_gate["reason"], reason)
                self.assertTrue(any(label in line for line in logs.output))
                if kind == "message":
                    scheduler.decide.assert_not_called()
                else:
                    self.assertLessEqual(scheduler.decide.call_count, 1)  # Persisted model cadence may still be active.

    def test_disconnected_gate_retains_both_queues_and_logs_only_changes(self):
        self.queue("bines_proactive", "working")
        self.queue("message")
        self.client.request.return_value = {"connected": False}
        scheduler = self.scheduler()
        with self.assertLogs("bines.qq.proactive") as logs:
            scheduler.tick()
            scheduler.tick()
        gates = [line for line in logs.output if "自主意图门控" in line]
        self.assertEqual(len(gates), 1)
        self.assertIn("QQ 连接断开", gates[0])
        self.assertIn("用户消息", gates[0])
        self.assertIn("自主任务", gates[0])
        self.client.request.return_value = {"connected": True}
        with self.assertLogs("bines.qq.proactive") as logs:
            scheduler.tick()
        self.assertTrue(any("用户消息待处理" in line for line in logs.output))

    def test_health_request_exception_becomes_disconnected(self):
        self.client.request.side_effect = ConnectionError("test")
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertEqual(scheduler.policies[self.conversation].state.last_gate["reason"], "disconnected")

    def test_legacy_policy_migrates_once_and_empty_new_state_does_not_resurrect_it(self):
        legacy = {"activity": {"id": "legacy"}, "contact_wait_until": 1234,
                  "weights": {"10": 0.4}, "interests": [{"topic": "test"}]}
        with self.store.connection() as db:
            db.execute("INSERT INTO proactive_sessions VALUES(?,?)", (self.conversation, json.dumps(legacy)))
        migrated, revision = self.store.proactive_state(self.conversation)
        self.assertEqual(migrated, legacy)
        self.store.autonomy_store().put("qq:" + self.conversation, {}, revision)
        self.assertEqual(self.store.proactive_state(self.conversation)[0], {})
        with self.store.connection() as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM proactive_sessions").fetchone()[0]), legacy)

    def test_pause_file_migrates_and_new_control_survives_restart(self):
        (self.directory / "autonomy_control.json").write_text(json.dumps({"mode": "pause"}))
        self.assertEqual(activity_control()["mode"], "pause")
        set_owner_control("stop", "test")
        self.assertEqual(AutonomyStore(directory=self.directory).get("control")[0]["mode"], "stop")
        self.assertEqual(json.loads((self.directory / "autonomy_control.json").read_text())["mode"], "pause")

    def test_failed_control_read_never_defaults_to_resume(self):
        with patch("common.activity.activity_store") as repo:
            repo.return_value.get.side_effect = AutonomyStoreError("offline")
            with self.assertRaises(AutonomyStoreError):
                ProactivePolicy(time.time(), self.config).tick(time.time(), None)

    def test_revision_rejects_stale_writer(self):
        first = self.store.autonomy_store()
        second = AutonomyStore(directory=self.directory, sqlite_path=getattr(first, "path", None))
        revision = first.put("test", {"value": 1}, 0)
        first.put("test", {"value": 2}, revision)
        with self.assertRaises(StateConflict):
            second.put("test", {"value": 3}, revision)
        self.assertEqual(second.get("test")[0], {"value": 2})

    def test_lease_excludes_second_scheduler_and_releases_after_error(self):
        first = self.store.autonomy_store()
        second = AutonomyStore(directory=self.directory, sqlite_path=getattr(first, "path", None))
        with first.scheduler("qq"):
            with self.assertRaises(StateConflict):
                with second.scheduler("qq"):
                    self.fail("duplicate scheduler")
        with second.scheduler("qq"):
            second.put("test", {}, 0)

    def test_handoff_restart_between_state_commit_and_ack_does_not_reapply(self):
        checkpoint = {"id": "goal", "kind": "curiosity", "mode": "review",
                      "topic": "test", "intent": "continue", "reason": "test",
                      "sequence": 3, "note": "saved"}
        self.store.begin_turn("owner:1")
        self.store.save_turn("owner:1", "done", {}, handoff={
            "conversation": self.conversation, "checkpoint": checkpoint,
        })
        scheduler = self.scheduler()
        policy = ProactivePolicy(time.time(), self.config)
        scheduler.consume_handoffs(self.conversation, policy, time.time())
        committed = self.store.proactive_state(self.conversation)[0]
        with self.store.connection() as db:
            db.execute("UPDATE activity_handoffs SET consumed=0")
        restored = ProactivePolicy(time.time(), self.config, committed)
        restored.accept_activity = Mock(side_effect=AssertionError("already applied"))
        scheduler.consume_handoffs(self.conversation, restored, time.time())
        restored.accept_activity.assert_not_called()
        self.assertEqual(restored.state.handoff_ref, "owner:1")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT consumed FROM activity_handoffs").fetchone()[0], 1)

    def test_failed_handoff_commit_keeps_queue_unacknowledged(self):
        self.store.begin_turn("owner:2")
        self.store.save_turn("owner:2", "done", {}, handoff={
            "conversation": self.conversation, "checkpoint": {"mode": "finish"},
        })
        scheduler = self.scheduler()
        with patch.object(scheduler.states, "put", side_effect=AutonomyStoreError("offline")):
            with self.assertRaises(AutonomyStoreError):
                scheduler.consume_handoffs(self.conversation, ProactivePolicy(time.time()), time.time())
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT consumed FROM activity_handoffs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
