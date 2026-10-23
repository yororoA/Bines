"""Continuous autonomy regression with simulated tools; no external posts."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from common.activity import activity_checkpoint
from common.proactive import ProactiveConfig, ProactivePolicy
from tools.context import ToolContext, tool_context
from chatBot.messages import LOCAL_IDENTITY
from thingking.src.will_runtime import WillSkillRunner, run_intention, ActivityYield
from tools.activity_tool import activity_continue, activity_finish, activity_pause, autonomy_control
from verification.proactive_behavior import QQTests, intention
from chatBot.proactive import QQProactive
from thingking.qq_thinking import QQThinking


class ContinuousPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.user = {"id": 1, "ts": self.now - 2000, "content": "先忙一会"}
        self.config = ProactiveConfig(min_idle=0, startup_grace=0, quiet_start=0, quiet_end=0)
        self.policy = ProactivePolicy(self.now, self.config)
        self.policy.tick(self.now, self.user)
        self.candidate = {**intention("curiosity"), "id": "test", "based_on_user": "1",
                          "timestamp": self.now, "contact_allowed": True}

    def test_continuation_preserves_drive_bypasses_threshold_and_counts_one_session(self):
        before = self.policy.state.drives["curiosity"]
        self.policy.reserve(self.candidate)
        checkpoint = activity_checkpoint(self.candidate, {
            "mode": "continue", "next_intent": "再读第二页", "delay_seconds": 2,
        }, [{"action": "browser_search", "result": "第一条资料", "status": "succeeded"}])
        self.policy.resolve(self.now, delivered=True, activity=checkpoint)
        self.assertEqual(self.policy.state.drives["curiosity"], before)
        self.assertEqual(self.policy.state.actions_today, 1)
        self.policy.state.drives = dict.fromkeys(self.policy.state.drives, 0.01)
        self.assertTrue(self.policy.tick(self.now + 3, self.user))
        next_candidate = {**self.candidate, "id": "test:2", "activity": self.policy.state.activity}
        self.policy.reserve(next_candidate)
        self.policy.resolve(self.now + 3, delivered=True, activity=checkpoint)
        self.assertEqual(self.policy.state.actions_today, 1)

    def test_checkpoint_survives_restart_and_sleep_pauses_execution(self):
        checkpoint = activity_checkpoint(self.candidate, {"mode": "continue"}, [])
        self.policy.reserve(self.candidate)
        self.policy.resolve(self.now, delivered=True, activity=checkpoint)
        restored = ProactivePolicy(self.now + 3, self.config, self.policy.dump())
        self.assertFalse(restored.tick(self.now + 4, self.user, life={"phase": "resting"}))
        self.assertTrue(restored.tick(self.now + 5, self.user, life={"phase": "active"}))
        self.assertEqual(restored.state.activity["intent"], self.candidate["intent"])

    def test_human_verification_pause_does_not_auto_resume(self):
        checkpoint = activity_checkpoint(self.candidate, {
            "mode": "pause", "delay_seconds": 0, "note": "扫码登录",
        }, [])
        self.policy.reserve(self.candidate)
        self.policy.resolve(self.now, delivered=True, activity=checkpoint)
        self.assertFalse(self.policy.tick(self.now + 5000, self.user))
        with patch("common.activity.activity_control", return_value={
            "mode": "resume", "updated_at": self.now + 5001,
        }):
            self.assertTrue(self.policy.tick(self.now + 5002, self.user))

    def test_uncertain_result_keeps_progress_paused_across_restart(self):
        checkpoint = activity_checkpoint(self.candidate, {
            "mode": "continue", "next_intent": "核对页面和原回执",
        }, [{"action": "website_interact", "result": "uncertain", "status": "uncertain"}])
        self.policy.reserve(self.candidate)
        self.policy.resolve(self.now, uncertain=True, activity=checkpoint)
        restored = ProactivePolicy(self.now + 5000, self.config, self.policy.dump())
        self.assertFalse(restored.tick(self.now + 5001, self.user))
        self.assertEqual(restored.state.activity["intent"], "核对页面和原回执")
        self.assertEqual(restored.state.activity["delay_seconds"], 0)


class ContinuousToolTests(unittest.TestCase):
    def test_more_than_eight_steps_are_allowed_and_quantum_yields(self):
        runner = Mock()
        runner.execute.return_value = '{"status":"ok"}'
        wrapper = WillSkillRunner(runner, {"contact_allowed": True}, lambda: "")
        with tool_context(ToolContext(LOCAL_IDENTITY, autonomous=True)):
            for _ in range(24):
                wrapper.execute({"action": "browser_search", "args": {}})
            with self.assertRaises(ActivityYield):
                wrapper.execute({"action": "browser_search", "args": {}})
        self.assertEqual(runner.execute.call_count, 24)

    def test_verification_pauses_before_another_tool_call(self):
        runner = Mock()
        runner.execute.return_value = json.dumps({"status": "blocked_human", "human_action_required": True})
        wrapper = WillSkillRunner(runner, {"contact_allowed": True}, lambda: "")
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        with tool_context(context):
            wrapper.execute({"action": "website_interact", "args": {}})
            with self.assertRaises(ActivityYield):
                wrapper.execute({"action": "website_interact", "args": {}})
        self.assertEqual(context.activity["delay_seconds"], 0)
        self.assertEqual(runner.execute.call_count, 1)

    def test_finish_continue_and_owner_controls_are_event_scoped(self):
        with tool_context(ToolContext(LOCAL_IDENTITY, autonomous=True)) as context:
            activity_continue("继续看下一段", delay_seconds=15)
            self.assertEqual(context.activity["mode"], "continue")
            activity_finish("看够了")
            self.assertEqual(context.activity["mode"], "finish")
            activity_pause("验证码", delay_seconds=0)
            self.assertEqual(context.activity["mode"], "pause")
            with self.assertRaises(PermissionError):
                autonomy_control("resume")
        with tempfile.TemporaryDirectory() as temp:
            with patch("common.activity.control_path", return_value=Path(temp) / "control.json"):
                with tool_context(ToolContext(LOCAL_IDENTITY, user_text="暂停自主活动")):
                    self.assertEqual(autonomy_control("pause")["control"]["mode"], "pause")
                with tool_context(ToolContext(LOCAL_IDENTITY, user_text="不要恢复自主活动")):
                    with self.assertRaisesRegex(PermissionError, "否定"):
                        autonomy_control("resume")


class QQContinuousTests(unittest.TestCase):
    setUp = QQTests.setUp
    tearDown = QQTests.tearDown
    insert = QQTests.insert
    prepare = QQTests.prepare

    def test_slow_active_worker_is_not_recovered_as_expired(self):
        event = self.prepare("curiosity")
        now = time.time()
        candidate = {**event["proactive"], "timestamp": now - 200}
        event["proactive"] = candidate
        with self.store.connection() as db:
            db.execute("UPDATE proactive_runs SET candidate=? WHERE id=?", (json.dumps(candidate), event["event_id"]))
            db.execute("UPDATE events SET status='working',lease=? WHERE id=?", (now + 120, event["event_id"]))
        from chatBot.proactive import guard
        self.assertEqual(guard(self.store, event, now), "")
        policy = ProactivePolicy(now)
        policy.reserve(candidate)
        scheduler = QQProactive(self.store, self.memory_hub, self.client)
        scheduler.recover(policy, now)
        self.assertEqual(policy.state.pending["id"], candidate["id"])
        self.assertEqual(policy.state.activity, {})

    def test_two_quanta_keep_one_activity_and_do_not_replay_completed_send(self):
        event = self.prepare("initiative")
        bot = QQThinking(self.store, self.client)

        class Model:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                runner.execute({"skill": "napcat", "action": "napcat_call",
                                "args": {"action": "send_qzone_msg", "params": {"content": "模拟说说"}}}, source="QQ")
                runner.execute({"skill": "activity", "action": "activity_continue",
                                "args": {"next_intent": "观察发表结果后继续浏览", "delay_seconds": 1}}, source="QQ")
                return "", [], []

        with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
            bot.process_proactive(event, Model())
        bot.process_proactive(event, Mock())
        self.assertEqual(sum(action == "send_qzone_msg" for action, _ in self.api.calls), 1)
        with self.store.connection() as db:
            payload = json.loads(db.execute("SELECT payload FROM turns WHERE id=?", (event["event_id"],)).fetchone()[0])
        self.assertEqual(payload["activity"]["mode"], "continue")
        self.assertEqual(payload["activity"]["intent"], "观察发表结果后继续浏览")
        now = time.time()
        policy = ProactivePolicy(now, ProactiveConfig(startup_grace=0, min_idle=0))
        policy.reserve(event["proactive"])
        scheduler = QQProactive(self.store, self.memory_hub, self.client)
        scheduler.recover(policy, now)
        self.assertEqual(policy.state.activity["mode"], "continue")
        self.assertNotIn("模拟说说", policy.state.activity["intent"])
        saved = dict(policy.state.activity)
        next_candidate = {**event["proactive"], "id": "proactive:test:next", "timestamp": time.time(),
                          "intent": saved["intent"], "activity": saved}
        next_event = {**event, "event_id": next_candidate["id"], "proactive": next_candidate}
        identity = self.store.identity(next_event)
        with self.store.connection() as db:
            db.execute("INSERT INTO proactive_runs(id,conversation,candidate) VALUES(?,?,?)",
                       (next_candidate["id"], identity.conversation, json.dumps(next_candidate)))
            db.execute("INSERT INTO events(id,conversation,raw,received) VALUES(?,?,?,?)",
                       (next_candidate["id"], identity.conversation, json.dumps(next_event), time.time()))

        class NextModel:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                self.assertIn("观察发表结果后继续浏览", conversation[-2]["content"])
                runner.execute({"skill": "napcat", "action": "napcat_call",
                                "args": {"action": "get_friend_list", "params": {}}}, source="QQ")
                runner.execute({"skill": "activity", "action": "activity_finish",
                                "args": {"note": "完成观察"}}, source="QQ")
                return "", [], []

        policy.reserve(next_candidate)
        with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
            bot.process_proactive(next_event, NextModel())
        scheduler.recover(policy, time.time())
        self.assertEqual(policy.state.activity, {})
        self.assertEqual(policy.state.actions_today, 1)
        self.assertEqual(sum(action == "send_qzone_msg" for action, _ in self.api.calls), 1)


if __name__ == "__main__":
    unittest.main()
