"""Independent intention generation with a single executor and fresh authority."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from chatBot.proactive import QQProactive
from chatBot.store import MemoryStore
from common.proactive import ProactiveConfig, ProactivePolicy, human_ref, inspect_decision, intent_key
from common.intent_queue import enqueue
import bored_detector


def goal(name, kind="curiosity"):
    return {"kind": kind, "topic": "同一个兴趣", "intent": "具体目标：" + name, "reason": "确实想了解：" + name}


class ParallelIntentionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bines-parallel-intents-")
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"BINES_ENV_FILE": "/dev/null", "BINES_AUTONOMY_BACKEND": "sqlite",
                                      "BINES_MEMORY_DIR": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        control = patch("common.activity.activity_control", return_value={})
        control.start()
        self.addCleanup(control.stop)
        self.now = 1794374400.0
        self.clock = Mock(side_effect=lambda: self.now)
        self.config = ProactiveConfig(startup_grace=0, min_idle=0, quiet_start=0, quiet_end=0)
        self.store = MemoryStore(Path(self.temp.name) / "gateway.db", owners={"1"})
        event = {"post_type": "message", "message_type": "private", "self_id": 99,
                 "user_id": 1, "message_id": 1, "time": self.now - 3600,
                 "sender": {"user_id": 1}, "message": [{"type": "text", "data": {"text": "测试消息"}}]}
        self.store.ingest(event)
        self.identity = self.store.identity(event)
        with self.store.connection() as db:
            db.execute("UPDATE events SET status='done'")
            self.user = dict(db.execute("SELECT * FROM messages").fetchone())
        self.memory = Mock()
        self.memory.activity_snapshot.return_value = {"life": {"phase": "active"}}
        self.memory.build_context.return_value = {"life": {"phase": "active"}}
        self.client = Mock()
        self.client.request.return_value = {"connected": True}
        self.policy = ProactivePolicy(self.now, self.config, {"user_ref": human_ref(self.user)})

    def scheduler(self, decisions):
        scheduler = QQProactive(self.store, self.memory, self.client, config=self.config,
                               decide=Mock(side_effect=decisions), appraise=Mock(return_value=None),
                               clock=self.clock)
        scheduler.capabilities = "read-only test capabilities"
        scheduler.policies[self.identity.conversation] = self.policy
        scheduler.save(self.identity.conversation, self.policy)
        return scheduler

    def reserve_running(self):
        pending = {**goal("正在看第一篇"), "id": "proactive:running", "timestamp": self.now - 20,
                   "based_on_user": human_ref(self.user), "activity_at": self.user["ts"],
                   "reply_at": self.user["ts"], "contact_allowed": True}
        self.policy.reserve(pending)
        event = {"post_type": "bines_proactive", "self_id": 99, "user_id": 1,
                 "message_type": "private", "proactive": pending}
        with self.store.connection() as db:
            db.execute("INSERT INTO proactive_runs(id,conversation,candidate) VALUES(?,?,?)",
                       (pending["id"], self.identity.conversation, json.dumps(pending)))
            db.execute("INSERT INTO events(id,conversation,raw,received,status,lease) VALUES(?,?,?,?,?,?)",
                       (pending["id"], self.identity.conversation, json.dumps(event), self.now - 20,
                        "working", self.now + 3600))
        return pending

    def test_running_worker_can_accumulate_other_intentions_without_dispatching_them(self):
        pending = self.reserve_running()
        scheduler = self.scheduler([goal("另一篇"), goal("换个方向", "initiative")])
        scheduler.tick()
        self.assertEqual(self.policy.state.pending["id"], pending["id"])
        self.assertEqual(len(self.policy.state.intention_queue), 1)
        self.now += 60
        scheduler.tick()
        self.assertEqual(len(self.policy.state.intention_queue), 2)
        materials = scheduler.decide.call_args_list[1].args[0]
        self.assertEqual(materials["ongoing_intention"]["intent"], pending["intent"])
        self.assertEqual(len(materials["queued_intentions"]), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE id LIKE 'proactive:%'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_finished_worker_dispatches_saved_goal_without_a_global_behavior_cooldown(self):
        pending = self.reserve_running()
        scheduler = self.scheduler([goal("第二个目标")])
        scheduler.tick()
        with self.store.connection() as db:
            db.execute("UPDATE proactive_runs SET status='completed',delivered_at=?", (self.now,))
            db.execute("UPDATE events SET status='done'")
        self.now += 2
        scheduler.tick()
        self.assertEqual(self.policy.state.pending["intent"], goal("第二个目标")["intent"])
        self.assertNotEqual(self.policy.state.pending["id"], pending["id"])
        self.assertEqual(self.policy.state.pending["based_on_user"], human_ref(self.user))
        self.assertEqual(scheduler.decide.call_count, 1)
        self.assertEqual(self.policy.state.intention_queue, [])

    def test_same_interest_different_goal_survives_manual_pause_and_deduplication(self):
        self.policy.state.activity = {**goal("需要登录"), "id": "paused", "mode": "pause",
                                      "delay_seconds": 0, "next_at": self.now}
        scheduler = self.scheduler([goal("读公开资料")])
        scheduler.tick()
        self.assertIn("paused", self.policy.state.waiting_activities)
        self.assertEqual(self.policy.state.pending["intent"], goal("读公开资料")["intent"])
        self.assertIsNone(self.policy.record_decision(goal("需要登录"), self.now + 1))

    def test_continuing_goal_does_not_skip_generation_and_short_wait_does_not_block_other_goal(self):
        old = {**goal("原方向"), "id": "continue", "mode": "continue", "delay_seconds": 5,
               "next_at": self.now + 5, "decision_source": "model"}
        self.policy.state.activity = old
        scheduler = self.scheduler([goal("新的方向")])
        scheduler.tick()
        self.assertEqual(len(self.policy.state.intention_queue), 1)
        self.now += 2
        scheduler.tick()
        self.assertEqual(self.policy.state.pending["intent"], goal("新的方向")["intent"])
        self.assertEqual(self.policy.state.activity["id"], "continue")
        self.assertEqual(scheduler.decide.call_count, 1)

    def test_repeated_contact_wait_only_changes_contact_deadline(self):
        self.policy.defer_generation(self.now, 900, wait_scope="contact")
        deadline = self.policy.state.contact_wait_until
        self.now += 60
        self.policy.defer_generation(self.now, 900, wait_scope="contact")
        self.assertEqual(self.policy.state.contact_wait_until, deadline)
        self.assertEqual(self.policy.state.next_intention_at, self.now + 60)
        self.now += 60
        self.policy.tick(self.now, self.user)
        self.assertTrue(self.policy.generation_due(self.now))

    def test_missing_wait_scope_is_review_and_does_not_make_a_long_global_rest(self):
        decision = inspect_decision('{"should_act":false,"delay_seconds":900}')
        self.assertEqual(decision["wait_scope"], "review")
        self.policy.defer_generation(self.now, 900, wait_scope=decision["wait_scope"])
        self.assertEqual(self.policy.state.intention_wait_until, 0)
        self.assertEqual(self.policy.state.next_intention_at, self.now + 60)

    def test_explicit_self_rest_survives_restart_but_new_outcome_reopens_a_review(self):
        self.policy.defer_generation(self.now, 900, wait_scope="all")
        restored = ProactivePolicy(self.now, self.config, self.policy.dump())
        self.assertFalse(restored.generation_due(self.now + 60))
        restored.state.outcome_revision += 1
        restored.tick(self.now + 60, self.user)
        self.assertTrue(restored.generation_due(self.now + 120))

    def test_owner_pause_stops_generation_and_execution(self):
        enqueue(self.policy.state, goal("已排队"), self.now)
        with patch("common.activity.activity_control", return_value={"mode": "pause"}):
            self.assertFalse(self.policy.tick(self.now, self.user))
            self.assertFalse(self.policy.generation_due(self.now))

    def test_queue_expires_old_goals_and_cancels_stale_contact_without_losing_independent_goals(self):
        self.policy.enqueue_intention(goal("旧联系", "connection"), self.now)
        self.policy.enqueue_intention(goal("独立活动"), self.now)
        self.policy.tick(self.now + 1, {**self.user, "id": "new", "external_id": "new"})
        self.assertEqual([row["kind"] for row in self.policy.state.intention_queue], ["curiosity"])
        self.policy.tick(self.now + 21601, self.user)
        self.assertEqual(self.policy.state.intention_queue, [])

    def test_queue_respects_limit_and_uncertain_receipts(self):
        for index in range(8):
            self.assertTrue(self.policy.enqueue_intention(goal(str(index)), self.now))
        self.assertFalse(self.policy.enqueue_intention(goal("overflow"), self.now))
        self.policy.state.recent_intents[intent_key(goal("0"))] = {"status": "uncertain", "at": self.now}
        chosen = self.policy.take_intention(self.now, self.user)
        self.assertEqual(chosen["intent"], goal("1")["intent"])

    def test_existing_activity_and_new_goals_alternate_without_losing_progress(self):
        self.policy.state.activity = {**goal("原方向"), "id": "old", "mode": "continue",
                                      "next_at": self.now, "sequence": 4, "decision_source": "model"}
        for name in ("新方向一", "新方向二"):
            self.policy.enqueue_intention(goal(name), self.now)
        first, checkpoint = self.policy.select_intention(self.now, self.user)
        self.assertEqual(first["intent"], goal("新方向一")["intent"])
        self.assertFalse(checkpoint)
        self.policy.reserve({**first, "id": "new", "timestamp": self.now})
        self.policy.resolve(self.now, delivered=True, activity={"mode": "finish"})
        second, checkpoint = self.policy.select_intention(self.now + 1, self.user)
        self.assertEqual(second["intent"], goal("原方向")["intent"])
        self.assertEqual(checkpoint["sequence"], 4)
        self.assertEqual(checkpoint["mode"], "review")
        self.assertEqual(len(self.policy.state.intention_queue), 1)

    def test_blocked_contact_in_queue_does_not_bypass_a_specific_activity_timer(self):
        self.policy.state.uncertain_contact_ref = self.policy.state.user_ref
        self.policy.state.activity = {**goal("还在等待"), "id": "old", "mode": "continue",
                                      "next_at": self.now + 30}
        self.policy.enqueue_intention(goal("联系", "connection"), self.now)
        self.assertEqual(self.policy.select_intention(self.now, self.user), (None, {}))

    def test_desktop_generation_preserves_running_candidate_and_saves_model_cadence_before_io(self):
        self.policy.reserve({**goal("正在执行"), "id": "working", "timestamp": self.now})
        saved = []
        with patch.object(bored_detector, "draft_decision", return_value=goal("新目标")), \
                patch.object(bored_detector, "appraise_history", return_value=None), \
                patch.object(bored_detector.time, "time", return_value=self.now):
            bored_detector.generate_intention(
                self.policy, self.memory, {"last_user": self.user, "life": {"phase": "active"}},
                "", self.now, lambda: saved.append(self.policy.dump()))
        self.assertEqual(saved[0]["next_intention_at"], self.now + 60)
        self.assertEqual(self.policy.state.pending["id"], "working")
        self.assertEqual(self.policy.state.intention_queue[0]["intent"], goal("新目标")["intent"])


if __name__ == "__main__":
    unittest.main()
