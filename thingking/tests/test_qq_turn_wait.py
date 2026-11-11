"""Durable follow-up waits with a fake model/transport and isolated SQLite only."""
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]
from chatBot.store import MemoryStore
from chatBot.turn_wait import FollowupRunner, TurnDeferred
from chatBot.messages import SEND_ACTIONS


def message(mid, text="a complete question", *, user="2", group="", addressed=False):
    chain = [{"type": "at", "data": {"qq": "99"}}] if addressed else []
    chain.append({"type": "text", "data": {"text": text}})
    return {"post_type": "message", "self_id": "99", "user_id": user,
            "message_type": "group" if group else "private", "group_id": group,
            "message_id": mid, "message": chain}


class Clock:
    now = 1_790_000_000.0

    def __call__(self):
        return self.now

    def advance(self, value):
        self.now += value


class BaseRunner:
    action_to_skill = {"synthetic_read": "synthetic"}

    def function_tools(self, *_a, **_kw):
        return [{"type": "function", "function": {"name": "synthetic_read"}}]

    def instructions_for_source(self, *_a, **_kw):
        return "static instructions"

    def execute(self, command, **_kwargs):
        return json.dumps({"status": "ok", "action": command["action"]})


class TurnWaitFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="bines-turn-wait-")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "gateway.db"
        self.clock = Clock()
        timer = patch("chatBot.store.time.time", self.clock)
        timer.start()
        self.addCleanup(timer.stop)
        self.store = MemoryStore(self.path, owners={"1"}, max_attempts=2)

    def claim(self, worker="worker"):
        return self.store.claim(worker, debounce=0, max_wait=0)

    def incoming(self, mid=1, **kwargs):
        value = message(mid, **kwargs)
        self.store.ingest(value)
        return value

    def row(self, event_id):
        with self.store.connection() as db:
            return dict(db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())


class TurnWaitStoreTests(TurnWaitFixture):
    def test_initial_complete_message_keeps_short_existing_debounce(self):
        self.incoming()
        self.assertEqual(self.store.claim("worker"), [])
        self.clock.advance(1.5)
        rows = self.store.claim("worker")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.turn_waits.state(rows[0]["batch"])["wait_count"], 0)

    def test_wait_restart_same_worker_lease_is_fenced_and_failure_budget_is_separate(self):
        self.incoming()
        old = self.claim()
        self.assertTrue(self.store.turn_waits.defer(old, seconds=6, semantic=True, reason="unfinished"))
        self.assertEqual(self.row(old[0]["id"])["attempts"], 1)
        self.assertEqual(self.row(old[0]["id"])["deferred_claims"], 1)
        self.assertEqual(self.claim(), [])
        self.store = MemoryStore(self.path, owners={"1"}, max_attempts=2)
        self.clock.advance(6)
        new = self.claim()
        self.assertEqual(new[0]["batch"], old[0]["batch"])
        self.assertEqual(new[0]["attempts"], 2)
        self.assertEqual(self.store.check_lease(old), "qq_lease_lost")
        self.assertEqual(self.store.check_lease(new), "")
        with self.assertRaisesRegex(RuntimeError, "qq_lease_lost"):
            self.store.turn_waits.defer(old)
        self.store.finish([new[0]["id"]], "worker", "temporary error", retryable=True)
        self.assertEqual(self.row(new[0]["id"])["status"], "pending")
        self.clock.advance(2)
        last = self.claim()
        self.store.finish([last[0]["id"]], "worker", "temporary error", retryable=True)
        self.assertEqual(self.row(last[0]["id"])["failure_reason"], "retry_exhausted")

    def test_followup_wakes_early_and_never_resets_deadline_or_count(self):
        self.incoming()
        first = self.claim()
        deadline = self.store.turn_waits.state(first[0]["batch"])["deadline"]
        self.store.turn_waits.defer(first, seconds=6, semantic=True)
        self.clock.advance(2)
        self.incoming(2, text="the rest of my sentence")
        self.assertEqual(self.store.claim("worker"), [])
        self.clock.advance(1.5)
        merged = self.store.claim("worker")
        self.assertEqual(len(merged), 2)
        self.assertEqual({row["batch"] for row in merged}, {first[0]["batch"]})
        self.assertTrue(self.store.turn_waits.defer(merged, seconds=6, semantic=True))
        self.clock.advance(6)
        resumed = self.claim()
        self.assertFalse(self.store.turn_waits.defer(resumed, seconds=6, semantic=True))
        state = self.store.turn_waits.state(first[0]["batch"])
        self.assertEqual((state["deadline"], state["wait_count"]), (deadline, 2))

    def test_deadline_caps_wait_and_freshness_refresh_does_not_consume_retry_budget(self):
        self.incoming()
        rows = self.claim()
        self.clock.advance(19)
        self.assertTrue(self.store.turn_waits.defer(rows, seconds=6, semantic=True))
        self.clock.advance(1)
        rows = self.claim()
        self.assertFalse(self.store.turn_waits.defer(rows, seconds=1, semantic=True))
        # The deadline bounds intentional waiting, never blocks accepting newer input.
        for mid in range(2, 7):
            self.incoming(mid)
            self.assertTrue(self.store.turn_waits.new_input(rows[-1]["id"]))
            self.assertEqual(self.store.turn_waits.supersede(rows), "merged")
            rows = self.claim()
        self.assertGreater(rows[0]["attempts"], self.store.max_attempts)
        self.assertEqual(rows[0]["attempts"] - rows[0]["deferred_claims"], 1)
        self.assertEqual(len(rows), 6)

    def test_wait_does_not_lock_other_group_sender_or_coalesce_proactive(self):
        self.incoming(group="10", addressed=True)
        first = self.claim()
        self.store.turn_waits.defer(first, seconds=6, semantic=True)
        self.incoming(2, group="10", user="3", addressed=True)
        other = self.claim("other")
        self.assertEqual(len(other), 1)
        self.assertNotEqual(other[0]["conversation"], first[0]["conversation"])
        autonomous = {**message(3, group="10"), "post_type": "bines_proactive", "proactive": {"id": "a"}}
        self.store.ingest(autonomous)
        self.assertFalse(self.store.turn_waits.new_input(first[0]["id"]))
        self.clock.advance(6)
        resumed = self.claim()
        self.assertEqual(len(resumed), 1)
        self.assertEqual(json.loads(resumed[0]["raw"])["post_type"], "message")

    def test_withdrawn_followup_and_self_message_do_not_cancel_reply(self):
        self.incoming()
        rows = self.claim()
        self.incoming(2)
        with self.store.connection() as db:
            db.execute("UPDATE messages SET recalled=1 WHERE external_id='99:p:2:2'")
        self.incoming(3, user="99")
        self.assertFalse(self.store.turn_waits.new_input(rows[0]["id"]))

    def test_receipts_forbid_wait_and_are_preserved_when_new_input_arrives(self):
        self.incoming(group="10", addressed=True)
        rows = self.claim()
        rows[0]["_response_trigger"] = rows[0]["queue_seq"]
        self.store.begin_turn(rows[0]["batch"])
        with self.store.connection() as db:
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)", (
                rows[0]["batch"] + ":tool:0", "synthetic_write", "{}", "ok", "{}", self.clock()))
        self.assertFalse(self.store.turn_waits.defer(rows, seconds=6, semantic=True))
        self.incoming(2, group="10")
        self.assertEqual(self.store.turn_waits.supersede(rows), "completed_with_receipts")
        self.assertEqual(self.row(rows[0]["id"])["status"], "done")
        following = self.claim()
        self.assertEqual(following[0]["response_requested"], rows[0]["queue_seq"])
        self.assertNotEqual(following[0]["batch"], rows[0]["batch"])

    def test_runtime_schema_and_first_instructions_stay_identical_across_wait_states(self):
        self.incoming()
        rows = self.claim()
        runner = FollowupRunner(BaseRunner(), self.store.turn_waits, rows, lambda: "")
        tools, rules, state = runner.function_tools(), runner.instructions_for_source(), runner.prompt_state()
        self.clock.advance(3)
        self.store.turn_waits.defer(rows, seconds=1, semantic=True)
        self.clock.advance(1)
        resumed = self.claim()
        runner2 = FollowupRunner(BaseRunner(), self.store.turn_waits, resumed, lambda: "")
        self.assertEqual(runner2.function_tools(), tools)
        self.assertEqual(runner2.instructions_for_source(), rules)
        self.assertNotEqual(runner2.prompt_state(), state)
        self.clock.advance(20)
        self.assertEqual(runner2.function_tools(), tools)
        self.assertEqual(runner2.instructions_for_source(), rules)
        self.assertFalse(runner2.prompt_state()["available"])

    def test_control_tool_validates_arguments_and_stops_the_model_turn(self):
        self.incoming()
        rows = self.claim()
        runner = FollowupRunner(BaseRunner(), self.store.turn_waits, rows, lambda: "")
        for seconds in (True, float("nan"), 0, 7, "3"):
            result = runner.execute({"action": runner.action, "args": {"seconds": seconds, "reason": "unfinished"}})
            self.assertEqual(json.loads(result)["status"], "failed")
        with self.assertRaises(TurnDeferred):
            runner.execute({"action": runner.action, "args": {"seconds": 3, "reason": "unfinished"}})
        self.assertEqual(self.row(rows[0]["id"])["status"], "pending")


class TurnWaitProcessTests(TurnWaitFixture):
    def setUp(self):
        super().setUp()
        import thingking.qq_thinking as qq
        self.qq = qq
        self.sent = []
        fixture = self

        class Client:
            def raw_api(self, action, params, **kwargs):
                if action not in SEND_ACTIONS:
                    raise AssertionError("unexpected external API: " + action)
                fixture.sent.append((action, params))
                return {"status": "ok", "data": {"message_id": 1000 + len(fixture.sent)}}

        for target, value in [("thingking.qq_thinking.SkillRunner", lambda *_a, **_kw: BaseRunner()),
                              ("thingking.qq_thinking.QQThinking.import_history", lambda *_: None),
                              ("thingking.qq_thinking.prompt_builder.build_conversation_messages", lambda *_: [
                                  {"role": "system", "content": "stable persona"}]),
                              ("chatBot.media.describe_images", lambda *_: ""),
                              ("chatBot.media.resolve_replies", lambda *_: ([], []))]:
            replacement = patch(target, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.bot = qq.QQThinking(self.store, Client(), memory_client=SimpleNamespace(build_context=lambda *_a, **_kw: {}))

    def process(self, rows, callback):
        fixture = self
        class Model:
            def run_skill_protocol(self, conversation, runner, **_kwargs):
                fixture.last_conversation = copy.deepcopy(conversation)
                runner.before_model_request()
                return callback(conversation, runner), None, None
        self.bot.process(rows, Model())
        self.store.finish([row["id"] for row in rows], "worker")

    def test_semantic_wait_resumes_with_both_current_messages_without_a_second_classifier(self):
        self.incoming(text="if I use this")
        rows = self.claim()
        self.process(rows, lambda _c, runner: runner.execute({"action": "wait_for_followup", "args": {
            "seconds": 6, "reason": "conditional sentence unfinished"}}))
        self.assertEqual(self.sent, [])
        self.assertFalse(self.store.turn_writes(rows[0]["batch"]))
        self.clock.advance(2)
        self.incoming(2, text="will it work offline?")
        self.clock.advance(1.5)
        new = self.store.claim("worker")
        self.process(new, lambda *_: "answer after the complete question")
        request = self.last_conversation[-1]["content"]
        self.assertIn("if I use this", request)
        self.assertIn("will it work offline?", request)
        self.assertEqual(len(self.sent), 1)

    def test_group_trigger_includes_unaddressed_continuation_but_unaddressed_group_stays_quiet(self):
        self.incoming(group="10", addressed=True, text="first half")
        self.incoming(2, group="10", text="second half")
        self.process(self.claim(), lambda *_: "complete")
        self.assertIn("second half", self.last_conversation[-1]["content"])
        self.assertEqual(len(self.sent), 1)
        self.incoming(3, group="10", text="unaddressed new conversation")
        self.process(self.claim(), lambda *_: self.fail("unaddressed message ran a model"))
        self.assertEqual(len(self.sent), 1)

    def test_new_input_during_generation_prevents_stale_send_and_refreshes_batch(self):
        self.incoming(text="the first input")
        rows = self.claim()
        def generating(*_):
            self.incoming(2, text="a correction")
            return "obsolete answer"
        self.process(rows, generating)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.row(rows[0]["id"])["status"], "pending")
        merged = self.claim()
        self.process(merged, lambda *_: "updated answer")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("a correction", self.last_conversation[-1]["content"])

    def test_executed_effect_is_not_replayed_and_bare_group_followup_keeps_original_trigger(self):
        self.incoming(group="10", addressed=True)
        rows = self.claim()
        def executing(*_):
            with self.store.connection() as db:
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)", (
                    rows[0]["batch"] + ":tool:0", "synthetic_write", "{}", "ok", "{}", self.clock()))
            self.incoming(2, group="10", text="one more part")
            return "obsolete answer"
        self.process(rows, executing)
        self.assertEqual(self.row(rows[0]["id"])["status"], "done")
        self.assertEqual(self.sent, [])
        new = self.claim()
        self.process(new, lambda *_: "reply to the followup")
        self.assertEqual(len(self.sent), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)

    def test_withdrawn_only_trigger_disables_continuation_after_wait(self):
        self.incoming(group="10", addressed=True)
        rows = self.claim()
        self.process(rows, lambda _c, runner: runner.execute({"action": "wait_for_followup", "args": {
            "seconds": 6, "reason": "unfinished input"}}))
        self.incoming(2, group="10")
        with self.store.connection() as db:
            db.execute("UPDATE messages SET recalled=1 WHERE external_id='99:10:1'")
        self.process(self.claim(), lambda *_: self.fail("withdrawn only trigger ran a model"))
        self.assertEqual(self.sent, [])

    def test_trigger_withdrawn_during_generation_finishes_without_dead_letter_or_send(self):
        self.incoming(group="10", addressed=True)
        rows = self.claim()
        def generating(*_):
            with self.store.connection() as db:
                db.execute("UPDATE messages SET recalled=1 WHERE external_id='99:10:1'")
            return "obsolete answer"
        self.process(rows, generating)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.row(rows[0]["id"])["status"], "done")

    def test_current_and_quoted_author_ids_are_distinct_without_forbidding_history_replies(self):
        value = message(100)
        value["message"].insert(0, {"type": "reply", "data": {"id": 80}})
        current = self.qq.current_input_text([value])
        self.assertIn("a complete question", current)
        self.assertNotIn("current_message_id", current)
        self.assertNotIn("quoted_message_ids", current)
        quote = message(80, "old bot words", user="99")
        text = self.qq.quoted_input_text(["引用消息 80 内容"], [quote], self.store.identity(value))
        self.assertEqual(len(text), 1)
        self.assertIn('"quoted_message_id": "80"', text[0])
        self.assertIn('"speaker_type": "Bines"', text[0])


class TurnWaitGatewayTests(TurnWaitFixture):
    def test_last_gateway_fence_rejects_new_send_but_allows_existing_receipt(self):
        from chatBot.gateway import GatewayHandler
        from chatBot.gateway_client import GatewayClient
        self.incoming()
        rows = self.claim()
        dispatched = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        server.token = "offline-test"
        server.memory_hub = None
        server.service = SimpleNamespace(store=self.store, call=lambda *args, **kwargs: (
            dispatched.append(args) or {"status": "ok", "data": {"message_id": 900}}))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        client = GatewayClient(f"http://127.0.0.1:{server.server_port}", token="offline-test")
        self.incoming(2)
        params = {"action": "send_private_msg", "params": {"user_id": "2", "message": "obsolete"},
                  "event_id": rows[0]["id"], "operation_id": rows[0]["batch"] + ":reply:0"}
        with self.assertRaisesRegex(RuntimeError, "qq_new_input"):
            client.request("/action", params, event_id=rows[0]["id"])
        self.assertEqual(dispatched, [])
        with self.store.connection() as db:
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)", (
                params["operation_id"], params["action"], "{}", "ok", "{}", self.clock()))
        self.assertEqual(client.request("/action", params, event_id=rows[0]["id"])["status"], "ok")
        self.assertEqual(len(dispatched), 1)


if __name__ == "__main__":
    unittest.main()
