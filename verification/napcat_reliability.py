"""Isolated retry/recovery/ingress regressions; no real QQ or model requests."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from contextlib import contextmanager
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]

from chatBot.store import MemoryStore
from chatBot.napcat_client import NapCatClient
from chatBot.recovery import backfill
from common.memory_journal import MemoryJournal, MemoryOutbox


def event(number=1, user="2"):
    return {"post_type": "message", "message_type": "private", "self_id": "99", "user_id": user,
            "message_id": number, "message": [{"type": "text", "data": {"text": "临时测试输入"}}]}


class DurableQueue(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "gateway.db", owners={"1"},
                                 max_attempts=3, retry_base=2, retry_max=3)

    def tearDown(self):
        self.temp.cleanup()

    def row(self, event_id):
        with self.store.connection() as db:
            return dict(db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def test_retry_backoff_ordering_exhaustion_and_error_history(self):
        with patch("chatBot.store.time.time", return_value=1000):
            self.store.ingest(event())
            rows = self.store.claim("first", 0, 0)
            key, batch = rows[0]["id"], rows[0]["batch"]
            self.store.finish([key], "first", "temporary failure", retryable=True)
            self.assertEqual(self.row(key)["next_attempt"], 1002)
            self.store.ingest(event(2))
            self.store.ingest(event(3, user="3"))
            other = self.store.claim("other", 0, 0)
            self.assertNotEqual(other[0]["conversation"], rows[0]["conversation"])
            self.store.finish([r["id"] for r in other], "other")
            self.assertEqual(self.store.claim("too-early", 0, 0), [])
        with patch("chatBot.store.time.time", return_value=1002):
            rows = self.store.claim("second", 0, 0)
            self.assertEqual([r["id"] for r in rows], [key])
            self.assertEqual(rows[0]["batch"], batch)
            self.store.finish([key], "second", "retry failed", retryable=True)
            self.assertEqual(self.row(key)["next_attempt"], 1005)
        with patch("chatBot.store.time.time", return_value=1005):
            self.store.claim("third", 0, 0)
            self.store.finish([key], "third", "last failure", retryable=True)
            self.assertEqual(self.row(key)["failure_reason"], "retry_exhausted")
            self.assertEqual(self.store.health()["dead_letters"], 1)
            following = self.store.claim("next", 0, 0)
            self.assertEqual(len(following), 1)
            self.assertNotEqual(following[0]["id"], key)

    def test_success_preserves_last_error_and_stale_worker_cannot_finish(self):
        with patch("chatBot.store.time.time", return_value=1000):
            self.store.ingest(event())
            rows = self.store.claim("one", 0, 0, lease=1)
            key = rows[0]["id"]
            self.store.finish([key], "one", "temporary", retryable=True)
        with patch("chatBot.store.time.time", return_value=1003):
            self.store.claim("two", 0, 0)
            self.store.finish([key], "one", "stale", retryable=True)
            self.assertEqual(self.row(key)["worker"], "two")
            self.store.finish([key], "two")
            self.assertEqual(self.row(key)["status"], "done")
            self.assertEqual(self.row(key)["last_error"], "temporary")
            self.assertIsNone(self.row(key)["error"])

    def test_any_started_uncertain_or_completed_write_blocks_retry_and_lease_replay(self):
        for index, status in enumerate(("started", "uncertain", "ok", "failed"), 1):
            with self.subTest(status=status), patch("chatBot.store.time.time", return_value=1000):
                self.store.ingest(event(index, user=str(index)))
                row = self.store.claim("worker", 0, 0, lease=1)[0]
                operation = row["batch"] + ":reply:0"
                self.store.begin_operation(operation, "send_private_msg", {"message": "只发送一次"})
                if status != "started":
                    self.store.complete_operation(operation, {"status": status})
                if index % 2:
                    self.store.finish([row["id"]], "worker", "network error", retryable=True)
                else:
                    with patch("chatBot.store.time.time", return_value=1002):
                        self.assertEqual(self.store.claim("replacement", 0, 0), [])
                self.assertEqual(self.row(row["id"])["failure_reason"], "side_effects")
                allowed, _ = self.store.begin_operation(operation, "send_private_msg", {"message": "只发送一次"})
                self.assertFalse(allowed)

    def test_done_turn_with_uncertain_receipt_still_becomes_dead_letter(self):
        with patch("chatBot.store.time.time", return_value=1000):
            self.store.ingest(event())
            row = self.store.claim("first", 0, 0, lease=1)[0]
            self.store.begin_turn(row["batch"])
            self.store.save_turn(row["batch"], "done", {"activity": {"mode": "pause"}})
            self.store.begin_operation(row["batch"] + ":reply:0", "send_private_msg", {})
        with patch("chatBot.store.time.time", return_value=1002):
            self.assertEqual(self.store.claim("second", 0, 0), [])
        self.assertEqual(self.row(row["id"])["failure_reason"], "side_effects")

    def test_external_tool_journal_also_blocks_retry(self):
        self.store.ingest(event())
        row = self.store.claim("worker", 0, 0)[0]
        outbox = MemoryOutbox(self.store.path)
        outbox.put("external-tool", "record_experience", {"record": {"turn_id": row["batch"], "category": "tool"}})
        self.store.finish([row["id"]], "worker", "temporary", retryable=True)
        self.assertEqual(self.row(row["id"])["failure_reason"], "side_effects")

    def test_heartbeat_is_aggregate_and_expires(self):
        with patch("chatBot.store.time.time", return_value=1000):
            self.store.heartbeat("not-exposed-worker-id", "starting")
            self.assertEqual(self.store.health()["workers_live"], 0)
            self.store.heartbeat("not-exposed-worker-id", "processing")
            self.assertEqual(self.store.health()["workers_live"], 1)
            self.assertNotIn("not-exposed-worker-id", json.dumps(self.store.health()))
        with patch("chatBot.store.time.time", return_value=1046):
            self.assertEqual(self.store.health()["workers_live"], 0)


class JournalRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "gateway.db"
        self.store = MemoryStore(self.path, owners={"1"})
        self.hub = SimpleNamespace(store=self.store, outbox=MemoryOutbox(self.path), remember_receipt=lambda *_: None)

    def tearDown(self):
        self.temp.cleanup()

    def records(self):
        with self.store.connection() as db:
            return [json.loads(row[0])["record"] for row in db.execute("SELECT params FROM memory_outbox")]

    def test_incremental_late_receipt_and_reopen_do_not_replay_history(self):
        self.store.ingest(event())
        row = self.store.claim("worker", 0, 0)[0]
        turn_id = row["batch"]
        self.store.begin_turn(turn_id)
        self.store.save_turn(turn_id, "done", {"outcomes": [{"action": "browser_search", "result": "历史资料"}]})
        self.assertEqual(backfill(self.hub)["queued"], 1)
        self.assertEqual(backfill(self.hub)["processed"], 0)
        operation = turn_id + ":reply:0"
        self.store.begin_operation(operation, "send_private_msg", {"message": "test"})
        self.assertEqual(backfill(self.hub)["queued"], 1)
        self.assertIn("uncertain", [r["status"] for r in self.records()])
        self.store.complete_operation(operation, {"status": "ok", "data": {"message_id": 9}})
        self.assertEqual(backfill(self.hub)["queued"], 1)
        native = [r for r in self.records() if r.get("legacy_operation")]
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0]["status"], "succeeded")
        self.store = MemoryStore(self.path, owners={"1"})
        self.hub.store = self.store
        self.assertEqual(backfill(self.hub)["processed"], 0)

    def test_late_receipt_repairs_only_unfinished_modern_journal_with_provenance(self):
        from chatBot.memory_hub import identity_envelope
        self.store.ingest(event())
        row = self.store.claim("worker", 0, 0)[0]
        journal = MemoryJournal(self.hub.outbox, identity_envelope(self.store.identity(event())), row["batch"],
                                candidate={"topic": "保留上下文"}, event_ids=[row["id"]])
        journal.source_message_refs = ["synthetic-source"]
        for index, status in enumerate(("started", "uncertain", "succeeded", "failed")):
            with self.subTest(status=status):
                op = row["batch"] + f":tool:{index}"
                key = journal.prefix + ":native:" + op
                self.store.begin_operation(op, "send_private_msg", {"message": "fixture"})
                journal._record(key, {"category": "tool", "action": "send_private_msg", "status": status,
                                      "result": {"status": "original"}, "custom_evidence": ["retained"]})
                self.store.complete_operation(op, {"status": "ok", "data": {"message_id": 900 + index}})
                backfill(self.hub)
                with self.store.connection() as db:
                    record = json.loads(db.execute("SELECT params FROM memory_outbox WHERE source_ref=?", (key,)).fetchone()[0])["record"]
                self.assertEqual(record["source_message_refs"], ["synthetic-source"])
                self.assertEqual(record["candidate"], {"topic": "保留上下文"})
                self.assertEqual(record["custom_evidence"], ["retained"])
                self.assertNotIn("legacy", record)
                if status in {"started", "uncertain"}:
                    self.assertEqual(record["status"], "succeeded")
                    self.assertEqual(record["result"]["data"]["message_id"], 900 + index)
                else:
                    self.assertEqual(record["status"], status)
                    self.assertEqual(record["result"], {"status": "original"})

    def test_existing_database_migration_is_bounded_and_resumable(self):
        # Simulate a database written before recovery triggers/cursors existed.
        with self.store.connection() as db:
            for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                db.execute(f'DROP TRIGGER "{name}"')
            db.execute("DELETE FROM memory_recovery_cursors")
            for i in range(120):
                raw = json.dumps(event(i), ensure_ascii=False)
                db.execute("INSERT INTO events(id,conversation,raw,received,batch) VALUES(?,?,?,?,?)",
                           (f"legacy-{i}", "conversation", raw, i, f"turn-{i}"))
                db.execute("INSERT INTO turns VALUES(?,'done',?)", (f"turn-{i}", json.dumps({"outcomes": [{"action": "read", "result": "旧记录"}]})))
        self.store = MemoryStore(self.path, owners={"1"})
        self.hub.store = self.store
        result = backfill(self.hub, limit=7)
        self.assertLessEqual(result["processed"], 7)
        self.assertLess(len(self.records()), 120)
        for _ in range(100):
            backfill(self.hub, limit=7)
        self.assertEqual(len(self.records()), 120)
        self.assertEqual(backfill(self.hub, limit=7)["processed"], 0)
        self.hub.store = MemoryStore(self.path, owners={"1"})
        self.assertEqual(backfill(self.hub, limit=7)["processed"], 0)

    def test_concurrent_completion_keeps_new_queue_revision(self):
        self.store.ingest(event())
        row = self.store.claim("worker", 0, 0)[0]
        backfill(self.hub)
        operation = row["batch"] + ":reply:0"
        self.store.begin_operation(operation, "send_private_msg", {"message": "test"})
        changed = []
        def complete(*_):
            if not changed:
                changed.append(True)
                self.store.complete_operation(operation, {"status": "ok"})
        self.hub.remember_receipt = complete
        backfill(self.hub)
        self.assertEqual(self.store.health()["recovery_pending"], 1)
        backfill(self.hub)
        self.assertEqual(self.records()[0]["status"], "succeeded")
        self.assertEqual(self.store.health()["recovery_pending"], 0)

    def test_recalled_legacy_source_is_not_reintroduced(self):
        self.store.ingest(event())
        row = self.store.claim("worker", 0, 0)[0]
        self.store.begin_turn(row["batch"])
        self.store.save_turn(row["batch"], "done", {"outcomes": [{"action": "read", "result": "撤回材料"}]})
        self.store.ingest({**event(), "post_type": "notice", "notice_type": "friend_recall"})
        backfill(self.hub)
        self.assertEqual(self.records(), [])

    def test_initial_seed_and_trigger_install_share_a_write_lock(self):
        from chatBot.recovery import initialize_recovery
        with self.store.connection() as db:
            for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                db.execute(f'DROP TRIGGER "{name}"')
            db.execute("DELETE FROM memory_recovery_cursors")
        original = self.store.connection
        attempted, blocked = [], []
        def trace(statement):
            if "SELECT COALESCE(MAX(rowid)" not in statement or attempted:
                return
            attempted.append(True)
            try:
                with sqlite3.connect(self.path, timeout=0.01) as writer:
                    writer.execute("INSERT INTO events(id,conversation,raw,received) VALUES('racing','test',?,1)",
                                   (json.dumps(event()),))
            except sqlite3.OperationalError as exc:
                blocked.append("locked" in str(exc))
        @contextmanager
        def observed_connection():
            with original() as db:
                db.set_trace_callback(trace)
                yield db
        with patch.object(self.store, "connection", observed_connection):
            initialize_recovery(self.store)
        self.assertEqual(attempted, [True])
        self.assertEqual(blocked, [True])
        with sqlite3.connect(self.path) as writer:
            writer.execute("INSERT INTO events(id,conversation,raw,received) VALUES('racing','test',?,1)",
                           (json.dumps(event()),))
        with original() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM memory_recovery_queue WHERE kind='event' AND source_id='racing'").fetchone())


class IngressRecovery(unittest.TestCase):
    def start_consumer(self, client):
        client._event_thread = threading.Thread(target=client._dispatch, daemon=True)
        client._event_thread.start()
        self.addCleanup(client.disconnect)

    def test_persistence_failure_retains_event_without_replaying_successful_listener(self):
        client = NapCatClient(persistence_retry_delay=0.01)
        first, second, done = [], [], threading.Event()
        client.add_event_listener(lambda value: first.append(value["message_id"]))
        def flaky(value):
            second.append(value["message_id"])
            if len(second) == 1:
                raise sqlite3.OperationalError("temporarily locked")
            done.set()
        client.add_event_listener(flaky)
        self.start_consumer(client)
        client._on_message(None, json.dumps(event()))
        self.assertTrue(done.wait(2))
        client.disconnect()
        self.assertEqual(first, [1])
        self.assertEqual(second, [1, 1])
        health = client.health()
        self.assertEqual(health["persistence_failures"], 1)
        self.assertEqual(health["persisted_events"], 1)
        self.assertEqual(health["pending_events"], 0)

    def test_full_queue_preserves_overflow_frame_and_reports_unbuffered_frames(self):
        client = NapCatClient(event_queue_limit=1)
        closed, seen, done = [], [], threading.Event()
        client.ws = SimpleNamespace(close=lambda: closed.append(True))
        def save(value):
            seen.append(value["message_id"])
            if len(seen) == 2:
                done.set()
        client.add_event_listener(save)
        for i in (1, 2, 3):
            client._on_message(client.ws, json.dumps(event(i)))
        self.assertEqual(client.health()["pending_events"], 2)
        self.assertEqual(client.health()["unbuffered_events"], 1)
        self.start_consumer(client)
        self.assertTrue(done.wait(2))
        self.assertEqual(seen, [1, 2])
        self.assertTrue(closed)


class ThinkingRetry(unittest.TestCase):
    def setUp(self):
        from verification.napcat_integration import Integration
        self.fixture = Integration()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_transient_model_failure_then_one_reply_and_persisted_completed_turn(self):
        from thingking.qq_thinking import QQThinking, retryable_failure
        fixture = self.fixture
        fixture.insert(event())
        rows = fixture.store.claim("first", 0, 0)
        bot = QQThinking(fixture.store, fixture.client)
        class Model:
            calls = 0
            def run_skill_protocol(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary model failure")
                return "已恢复", [], []
        model = Model()
        try:
            bot.process(rows, model)
        except RuntimeError as exc:
            fixture.store.finish([rows[0]["id"]], "first", str(exc), retryable=retryable_failure(exc))
        with fixture.store.connection() as db:
            db.execute("UPDATE events SET next_attempt=0")
        retry = fixture.store.claim("second", 0, 0)
        self.assertEqual(retry[0]["batch"], rows[0]["batch"])
        bot.process(retry, model)
        fixture.store.finish([retry[0]["id"]], "second")
        self.assertEqual(model.calls, 2)
        sends = [call for call in fixture.api.calls if call[0].startswith("send_")]
        self.assertEqual(len(sends), 1)
        with fixture.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM turns WHERE id=?", (rows[0]["batch"],)).fetchone()[0], "done")

    def test_model_failure_after_qq_write_becomes_dead_letter(self):
        from thingking.qq_thinking import QQThinking, retryable_failure
        fixture = self.fixture
        fixture.insert(event())
        rows = fixture.store.claim("first", 0, 0)
        class Model:
            def run_skill_protocol(self, *_args, **_kwargs):
                fixture.client.raw_api("send_private_msg", {"user_id": "2", "message": "已发送"})
                raise RuntimeError("model stream interrupted after send")
        with self.assertRaises(RuntimeError) as caught:
            QQThinking(fixture.store, fixture.client).process(rows, Model())
        fixture.store.finish([rows[0]["id"]], "first", str(caught.exception),
                             retryable=retryable_failure(caught.exception))
        self.assertEqual(fixture.store.claim("second", 0, 0), [])
        self.assertEqual(fixture.store.health()["dead_letter_reasons"], {"side_effects": 1})
        self.assertEqual(len([call for call in fixture.api.calls if call[0] == "send_private_msg"]), 1)

    def test_stale_worker_cannot_execute_tool_after_another_worker_claims(self):
        from thingking.qq_thinking import QQThinking
        fixture = self.fixture
        fixture.insert(event())
        rows = fixture.store.claim("first", 0, 0)
        bot = QQThinking(fixture.store, fixture.client)
        effects, replacement = [], []
        bot.runner.registry["browser_search"] = lambda **_: effects.append(True)
        class Model:
            def run_skill_protocol(self, _messages, runner, **_kwargs):
                with fixture.store.connection() as db:
                    db.execute("UPDATE events SET lease=0")
                replacement.extend(fixture.store.claim("second", 0, 0))
                runner.execute({"skill": "web-research", "action": "browser_search", "args": {"query": "fixture"}})
                return "stale reply", [], []
        with self.assertRaisesRegex(RuntimeError, "qq_lease_lost"):
            bot.process(rows, Model())
        self.assertTrue(replacement)
        self.assertEqual(fixture.store.check_lease(replacement), "")
        self.assertEqual(fixture.store.check_lease(rows), "qq_lease_lost")
        self.assertEqual(effects, [])
        self.assertFalse(any(call[0].startswith("send_") for call in fixture.api.calls))

    def test_stale_worker_cannot_send_final_reply_or_replace_current_turn(self):
        from thingking.qq_thinking import QQThinking
        fixture = self.fixture
        fixture.insert(event())
        rows = fixture.store.claim("first", 0, 0)
        class Model:
            def run_skill_protocol(self, *_args, **_kwargs):
                with fixture.store.connection() as db:
                    db.execute("UPDATE events SET lease=0")
                self.replacement = fixture.store.claim("second", 0, 0)
                fixture.store.save_turn(rows[0]["batch"], "started", {"generation": "second"})
                return "stale reply", [], []
        model = Model()
        with self.assertRaisesRegex(RuntimeError, "qq_lease_lost"):
            QQThinking(fixture.store, fixture.client).process(rows, model)
        self.assertTrue(model.replacement)
        self.assertFalse(any(call[0].startswith("send_") for call in fixture.api.calls))
        with fixture.store.connection() as db:
            payload = json.loads(db.execute("SELECT payload FROM turns WHERE id=?", (rows[0]["batch"],)).fetchone()[0])
        self.assertEqual(payload, {"generation": "second"})

    def test_heartbeat_and_ack_failure_do_not_kill_consumer(self):
        from thingking.qq_thinking import QQThinking
        fixture = self.fixture
        fixture.insert(event(1, user="2"))
        fixture.insert(event(2, user="3"))
        bot = QQThinking(fixture.store, fixture.client, model_factory=lambda: object())
        processed = []
        def process(rows, _model):
            processed.extend(row["id"] for row in rows)
            fixture.store.begin_turn(rows[0]["batch"])
            fixture.store.save_turn(rows[0]["batch"], "done", {})
            if len(processed) == 2:
                bot.stop.set()
        original_finish = fixture.store.finish
        finished = []
        def finish(*args, **kwargs):
            finished.append(True)
            if len(finished) == 1:
                raise sqlite3.OperationalError("temporary ack failure")
            return original_finish(*args, **kwargs)
        with patch.object(bot, "process", process), patch.object(fixture.store, "finish", finish), \
                patch.object(fixture.store, "heartbeat", side_effect=sqlite3.OperationalError("temporary heartbeat failure")), \
                patch("thingking.qq_thinking.DEBOUNCE", 0), patch("thingking.qq_thinking.MAX_WAIT", 0), \
                self.assertLogs("bines.qq.thinking", level="ERROR"):
            thread = threading.Thread(target=bot.worker, daemon=True)
            thread.start()
            thread.join(3)
            bot.stop.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(processed), 2)
        self.assertEqual(len(finished), 2)
        with fixture.store.connection() as db:
            db.execute("UPDATE events SET lease=0 WHERE status='working'")
        self.assertEqual(fixture.store.claim("recovery", 0, 0), [])
        self.assertEqual(fixture.store.health()["events"].get("done"), 2)
        self.assertEqual(fixture.store.health()["dead_letters"], 0)


if __name__ == "__main__":
    unittest.main()
