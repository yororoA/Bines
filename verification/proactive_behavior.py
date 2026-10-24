"""Willingness and fake-NapCat regression; no real outbound calls."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]
from common.proactive import (
    ProactiveConfig, ProactivePolicy, capability_summary, contact_boundary, context_blocked,
    human_ref, inspect_decision, parse_appraisal, parse_decision,
)
from chatBot.proactive import QQProactive, guard
from verification.napcat_integration import Integration, event
from thingking.qq_thinking import QQThinking
from chatBot.messages import LOCAL_IDENTITY
from tools.context import ToolContext, tool_context
from will_runtime import WillSkillRunner


def stamp(hour=12, minute=0):
    return datetime(2026, 10, 22, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def intention(kind="connection"):
    return {"kind": kind, "topic": "螃蟹横着走", "intent": "查一下螃蟹横着走的原因并分享",
            "reason": "近期聊过螃蟹，现在有一点好奇"}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = stamp()
        self.config = ProactiveConfig(interval=60, min_idle=0, startup_grace=0)
        self.user = {"id": 1, "ts": self.now-1000, "content": "我喜欢螃蟹"}
        self.policy = ProactivePolicy(self.now, self.config)
        self.policy.tick(self.now, self.user)

    def candidate(self, kind="connection"):
        return {**intention(kind), "id": "candidate", "based_on_user": human_ref(self.user),
                "timestamp": self.now, "contact_allowed": True}

    def test_unset_limits_allow_activity_after_large_persisted_counts(self):
        with patch.dict(os.environ):
            os.environ.pop("BINES_PROACTIVE_DAILY_LIMIT", None)
            os.environ.pop("BINES_PROACTIVE_ACTION_DAILY_LIMIT", None)
            config = ProactiveConfig.from_env()
        self.assertEqual((config.daily_limit, config.action_daily_limit), (0, 0))
        restored = ProactivePolicy(self.now, config, {
            "user_ref": human_ref(self.user), "sent_today": 1000, "actions_today": 1000,
        })
        self.assertEqual(restored.eligible_kinds(self.now, self.user),
                         ["connection", "curiosity", "initiative"])

    def test_positive_limits_apply_only_to_their_activity_kind(self):
        with patch.dict(os.environ, {
            "BINES_PROACTIVE_DAILY_LIMIT": "2",
            "BINES_PROACTIVE_ACTION_DAILY_LIMIT": "3",
        }):
            policy = ProactivePolicy(self.now, ProactiveConfig.from_env())
        policy.state.sent_today, policy.state.actions_today = 2, 2
        self.assertEqual(policy.eligible_kinds(self.now, self.user), ["curiosity", "initiative"])
        policy.state.sent_today, policy.state.actions_today = 1, 3
        self.assertEqual(policy.eligible_kinds(self.now, self.user), ["connection"])
        policy.state.sent_today = 2
        self.assertEqual(policy.eligible_kinds(self.now, self.user), [])

    def test_negative_limits_remain_invalid(self):
        for name in ("BINES_PROACTIVE_DAILY_LIMIT", "BINES_PROACTIVE_ACTION_DAILY_LIMIT"):
            with patch.dict(os.environ, {name: "-1"}):
                with self.assertRaisesRegex(ValueError, "次数配置无效"):
                    ProactiveConfig.from_env()

    def test_willingness_is_bounded_and_approaches_targets_not_timer_100(self):
        for step in range(1, 501):
            self.policy.tick(self.now+step*10, self.user)
        self.assertLess(self.policy.state.value, 100)
        self.assertTrue(all(0 <= v <= 1 for v in self.policy.state.drives.values()))

    def test_emotion_changes_different_drives(self):
        other = ProactivePolicy(self.now, self.config, self.policy.dump())
        for policy, social, curiosity in ((self.policy, 1, 0), (other, 0, 1)):
            policy.appraise({"emotion": {"valence": 0.8, "arousal": 0.7, "social": social, "curiosity": curiosity},
                             "interests": ["螃蟹"]}, "1", self.now)
            for step in range(1, 31):
                policy.tick(self.now+step*10, self.user)
        self.assertGreater(self.policy.state.drives["connection"], other.state.drives["connection"])
        self.assertLess(self.policy.state.drives["curiosity"], other.state.drives["curiosity"])

    def test_old_emotion_relaxes_towards_neutral(self):
        self.policy.appraise({"emotion": {"valence": 1, "arousal": 1, "social": 1, "curiosity": 1},
                             "interests": []}, "1", self.now-86400)
        self.policy.tick(self.now+30, self.user)
        self.assertLess(self.policy.state.drives["curiosity"], 0.45)

    def test_offline_has_no_elapsed_burst_and_observes_human_in_cooldown(self):
        self.policy.defer(self.now)
        new = {**self.user, "id": 2, "ts": self.now+2}
        self.policy.tick(self.now+3, new, available=False)
        self.assertEqual(self.policy.state.user_ref, "2")
        before = dict(self.policy.state.drives)
        self.policy.tick(self.now+3600, new, available=False)
        self.assertLess(max(abs(self.policy.state.drives[k]-v) for k, v in before.items()), 0.25)

    def test_unanswered_contact_only_suppresses_contact_even_after_restart(self):
        self.policy.reserve(self.candidate())
        self.policy.resolve(self.now, delivered=True)
        policy = ProactivePolicy(self.now+3600, self.config, self.policy.dump())
        self.assertNotIn("connection", policy.eligible_kinds(self.now+3600, self.user))
        self.assertIn("curiosity", policy.eligible_kinds(self.now+3600, self.user))
        new = {**self.user, "id": 2, "ts": self.now+3601}
        policy.tick(self.now+3601, new)
        self.assertIn("connection", policy.eligible_kinds(self.now+3601, new))

    def test_quiet_goodnight_and_daily_limit_do_not_turn_off_exploration(self):
        user = {**self.user, "content": "晚安，我去睡觉了"}
        self.assertNotIn("connection", self.policy.eligible_kinds(self.now, user))
        self.assertIn("initiative", self.policy.eligible_kinds(self.now, user))
        self.assertNotIn("connection", self.policy.eligible_kinds(stamp(7), self.user))
        self.assertFalse(context_blocked("我想听睡觉的故事"))

    def test_fulfilment_reduces_corresponding_drive_and_retains_history(self):
        before = self.policy.state.drives["curiosity"]
        self.policy.reserve(self.candidate("curiosity"))
        self.policy.resolve(self.now, delivered=True)
        self.assertLess(self.policy.state.drives["curiosity"], before)
        self.assertEqual(self.policy.state.actions_today, 1)
        self.assertEqual(self.policy.state.sent_today, 0)
        self.assertEqual(self.policy.state.recent_outcomes[-1]["outcome"], "completed")

    def test_hour_learning_crosses_hour_and_exact_timeout_once(self):
        sent = stamp(12, 59)+45
        self.policy.reserve(self.candidate())
        self.policy.resolve(sent, delivered=True)
        self.policy.tick(sent+30, {**self.user, "id": 2, "ts": sent+30})
        self.assertAlmostEqual(self.policy.state.weights["12"], 1.1)
        self.assertNotIn("13", self.policy.state.weights)
        self.policy.reserve(self.candidate())
        self.policy.resolve(self.now, delivered=True)
        self.policy.tick(self.now+3600, self.user)
        value = self.policy.state.weights["12"]
        self.policy.tick(self.now+7200, self.user)
        self.assertEqual(value, self.policy.state.weights["12"])

    def test_strict_intent_and_emotion_parser(self):
        self.assertIsNone(parse_decision("not true"))
        self.assertIsNone(parse_decision(json.dumps({"should_act": "true", **intention()})))
        self.assertIsNotNone(parse_decision(json.dumps({"should_act": True, **intention()})))
        self.assertIsNotNone(parse_decision(json.dumps({"should_act": True, **intention()}), ["螃蟹 横着走！"]))
        self.assertIsNone(parse_appraisal('{"emotion":{"social":"高"}}'))
        self.assertIsNotNone(parse_appraisal(json.dumps({"emotion": {
            "valence": 0.1, "arousal": 0.2, "social": 0.3, "curiosity": 0.4}, "interests": ["螃蟹"]})))

    def test_uncertain_action_is_not_automatically_repeated(self):
        self.policy.reserve(self.candidate("initiative"))
        self.policy.resolve(self.now, uncertain=True)
        self.assertIn(self.candidate()["topic"], self.policy.state.topics)
        self.assertEqual(self.policy.state.recent_outcomes[-1]["outcome"], "uncertain")

    def test_default_low_drive_and_waking_phase_still_get_model_opportunity(self):
        policy = ProactivePolicy(stamp(9), state={"user_ref": "1", "drives": dict.fromkeys(
            ("connection", "curiosity", "initiative"), 0.01)})
        user = {**self.user, "ts": stamp(9)-1000}
        self.assertTrue(policy.tick(stamp(9)+121, user, life={"phase": "waking"}))
        self.assertLess(policy.state.value, 45)
        self.assertEqual(policy.state.last_gate["reason"], "opportunity")

    def test_independent_desktop_start_does_not_require_human_history(self):
        policy = ProactivePolicy(self.now)
        self.assertFalse(policy.tick(self.now, None))
        self.assertEqual(policy.state.last_gate["reason"], "startup_grace")
        self.assertTrue(policy.tick(self.now+121, None))
        self.assertNotIn("connection", policy.state.last_gate["allowed_kinds"])

    def test_busy_sleep_and_explicit_dnd_boundaries_have_distinct_lifetimes(self):
        busy = {**self.user, "ts": self.now, "content": "我先忙"}
        self.assertTrue(contact_boundary(busy, self.now+3599, self.config))
        self.assertFalse(contact_boundary(busy, self.now+3600, self.config))
        sleep = {**self.user, "ts": stamp(2), "content": "晚安"}
        self.assertTrue(contact_boundary(sleep, stamp(7), self.config))
        self.assertFalse(contact_boundary(sleep, stamp(8), self.config))
        self.assertFalse(contact_boundary(sleep, stamp(7), self.config, stamp(6)))
        dnd = {**sleep, "content": "不要打扰"}
        self.assertEqual(contact_boundary(dnd, stamp(12)+86400, self.config, stamp(11)), "do_not_disturb")
        goodbye = {**busy, "content": "拜拜"}
        self.assertFalse(contact_boundary(goodbye, self.now+1800, self.config))

    def test_unanswered_contact_reopens_only_after_two_hours_and_uncertain_stays_blocked(self):
        self.policy.reserve(self.candidate())
        self.policy.resolve(self.now, delivered=True)
        self.assertNotIn("connection", self.policy.eligible_kinds(self.now+7199, self.user))
        self.assertIn("connection", self.policy.eligible_kinds(self.now+7200, self.user))
        self.assertTrue(self.policy.contact_permissions(self.now+10, self.user)["contact_allowed"])
        self.policy.reserve(self.candidate())
        self.policy.resolve(self.now+7201, uncertain=True)
        restored = ProactivePolicy(self.now+86400, self.config, self.policy.dump())
        self.assertNotIn("connection", restored.eligible_kinds(self.now+86400, self.user))

    def test_contact_idle_does_not_prevent_independent_thinking(self):
        policy = ProactivePolicy(self.now, ProactiveConfig(startup_grace=0), {"user_ref": "1"})
        recent = {**self.user, "ts": self.now-11}
        self.assertTrue(policy.tick(self.now, recent))
        self.assertNotIn("connection", policy.eligible_kinds(self.now, recent))
        self.assertIn("curiosity", policy.eligible_kinds(self.now, recent))

    def test_rest_planning_is_throttled_and_does_not_grant_public_authority(self):
        self.assertTrue(self.policy.tick(self.now+1, self.user, life={"phase": "resting"}))
        self.assertFalse(self.policy.contact_permissions(self.now+1, self.user)["contact_allowed"])
        candidate = {**self.candidate("curiosity"), "planning_only": True, "timestamp": self.now+1}
        self.policy.reserve(candidate)
        self.policy.resolve(self.now+2, delivered=True, activity={
            "mode": "review", "intent": "考虑是否恢复", "topic": "休息", "delay_seconds": 2})
        self.assertFalse(self.policy.tick(self.now+300, self.user, life={"phase": "resting"}))
        self.assertEqual(self.policy.state.last_gate["reason"], "rest_review_cooldown")
        self.assertTrue(self.policy.tick(self.now+1801, self.user, life={"phase": "resting"}))
        self.assertTrue(self.policy.tick(self.now+1802, self.user, life={"phase": "active"}))
        self.assertFalse(self.policy.state.planning_only)

    def test_resting_quiet_or_failed_model_call_also_backs_off(self):
        self.policy.tick(self.now+1, self.user, life={"phase": "resting"})
        self.policy.defer(self.now+1, 30)
        self.assertFalse(self.policy.tick(self.now+31, self.user, life={"phase": "resting"}))
        self.assertEqual(self.policy.state.next_decision, self.now+1801)

    def test_same_interest_new_goal_and_completed_goal_ttl(self):
        candidate = self.candidate("curiosity")
        self.policy.reserve(candidate)
        self.policy.resolve(self.now, delivered=True)
        original = json.dumps({"should_act": True, **intention("curiosity")})
        self.assertEqual(inspect_decision(original, recent_intents=self.policy.state.recent_intents,
                                         now=self.now+5)["status"], "duplicate")
        changed = json.dumps({"should_act": True, **intention("curiosity"), "intent": "看看螃蟹会不会游泳"})
        self.assertEqual(inspect_decision(changed, recent_intents=self.policy.state.recent_intents,
                                         now=self.now+5)["status"], "selected")
        self.assertEqual(inspect_decision(original, recent_intents=self.policy.state.recent_intents,
                                         now=self.now+21600)["status"], "selected")
        self.policy.reserve(candidate)
        self.policy.resolve(self.now+21601, uncertain=True)
        self.assertEqual(inspect_decision(original, recent_intents=self.policy.state.recent_intents,
                                         now=self.now+86400)["status"], "duplicate")

    def test_quiet_invalid_and_selected_model_decisions_are_observable(self):
        quiet = inspect_decision('{"should_act":false,"reason":"想休息一下","delay_seconds":900}')
        self.assertEqual((quiet["status"], quiet["delay_seconds"]), ("quiet", 900))
        self.assertEqual(inspect_decision("bad")["status"], "invalid")
        self.assertEqual(inspect_decision(json.dumps({"should_act": True, **intention()}))["status"], "selected")

    def test_model_changed_focus_updates_feedback_interest_and_appraisal(self):
        self.policy.appraise({"emotion": {"valence": 0, "arousal": .5, "social": .5, "curiosity": .5},
                             "interests": ["螃蟹"]}, "1", self.now)
        self.policy.reserve(self.candidate("curiosity"))
        self.policy.resolve(self.now+5, delivered=True, activity={
            "mode": "continue", "topic": "海鸟", "intent": "搜索海鸟如何捕食", "delay_seconds": 5,
            "decision": {"interest_level": .8}})
        self.assertEqual(self.policy.state.recent_outcomes[-1]["topic"], "海鸟")
        self.assertEqual(self.policy.state.interests[0], "海鸟")
        self.assertFalse(self.policy.appraisal_due("1", self.now+20))
        self.assertTrue(self.policy.appraisal_due("1", self.now+301))
        self.assertTrue(self.policy.appraisal_due("2", self.now+20))

    def test_capability_summary_contains_actual_website_and_control_actions(self):
        summary = capability_summary("QQ")
        self.assertIn("website_watch", summary)
        self.assertIn("activity_continue", summary)
        self.assertIn("regulate_life", summary)
        self.assertNotIn("left_click", summary)

    def test_nonfinite_or_negative_new_time_config_is_rejected(self):
        for name, value in (("BINES_PROACTIVE_THINKING_IDLE", "-1"),
                            ("BINES_PROACTIVE_REST_INTERVAL", "nan"),
                            ("BINES_PROACTIVE_INTERVAL", "inf")):
            with patch.dict(os.environ, {name: value}), self.assertRaises(ValueError):
                ProactiveConfig.from_env()


class WillToolsTests(unittest.TestCase):
    def test_planning_turn_exposes_only_internal_tools_and_blocks_external_execution(self):
        runner = Mock()
        runner.function_tools.return_value = [{"function": {"name": name}} for name in
                                             ("regulate_life", "activity_pause", "website_open", "napcat_call")]
        wrapper = WillSkillRunner(runner, {"planning_only": True, "contact_allowed": False}, lambda: "")
        self.assertEqual([tool["function"]["name"] for tool in wrapper.function_tools()],
                         ["regulate_life", "activity_pause"])
        for name in ("website_open", "browser_search", "napcat_call", "open_application", "mcp_call"):
            self.assertIn("未执行", wrapper.execute({"action": name}))
        wrapper.execute({"action": "regulate_life"})
        runner.execute.assert_called_once()

    def test_desktop_activity_and_cancellation_guard(self):
        runner = Mock()
        wrapper = WillSkillRunner(runner, {"contact_allowed": True}, lambda: "", desktop=True)
        with patch("will_runtime.desktop_idle_seconds", return_value=1):
            self.assertIn("电脑正在使用", wrapper.execute({"action": "open_application"}))
        runner.execute.assert_not_called()
        with patch("will_runtime.desktop_idle_seconds", return_value=120):
            wrapper.execute({"action": "open_application"})
        runner.execute.assert_called_once()
        with self.assertRaises(RuntimeError):
            WillSkillRunner(runner, {}, lambda: "new_user").execute({"action": "browser_search"})

    def test_no_contact_intention_still_can_search(self):
        runner = Mock()
        wrapper = WillSkillRunner(runner, {"contact_allowed": False}, lambda: "")
        self.assertIn("未执行", wrapper.execute({"action": "napcat_send"}))
        wrapper.execute({"action": "browser_search"})
        runner.execute.assert_called_once()


class QQTests(unittest.TestCase):
    def setUp(self):
        # Real wall-clock time must not turn a transport/recovery test into a
        # quiet-hours or sleep test when the suite runs after midnight.
        environment = patch.dict(os.environ, {
            "BINES_PROACTIVE_QUIET_START": "0", "BINES_PROACTIVE_QUIET_END": "0",
            "BINES_LIFE_SLEEP_START": "0", "BINES_LIFE_WAKE_HOUR": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        Integration.setUp(self)

    tearDown = Integration.tearDown
    insert = Integration.insert

    def prepare(self, kind="connection", contact=True):
        original = event("1", msg_id=501, time=time.time()-1800, text="螃蟹真好吃")
        self.insert(original)
        identity = self.store.identity(original)
        with self.store.connection() as db:
            db.execute("UPDATE events SET status='done'")
            user = dict(db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 1").fetchone())
        self.memory_hub.sync()
        candidate = {**intention(kind), "id": "proactive:test", "timestamp": time.time(),
                     "based_on_user": human_ref(user), "activity_at": user["ts"], "contact_allowed": contact}
        ev = {"self_id": 99, "user_id": "1", "post_type": "bines_proactive",
              "message_type": "private", "event_id": candidate["id"], "proactive": candidate}
        with self.store.connection() as db:
            db.execute("INSERT INTO proactive_runs(id,conversation,candidate) VALUES(?,?,?)",
                       (candidate["id"], identity.conversation, json.dumps(candidate)))
            db.execute("INSERT INTO events(id,conversation,raw,received) VALUES(?,?,?,?)",
                       (candidate["id"], identity.conversation, json.dumps(ev), time.time()))
        return ev

    def model(self, execute=None, text="螃蟹横着走的样子真神气"):
        class Model:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                self.assertEqual(conversation[-1]["role"], "user")
                self.assertIn("[Internal Scheduler Trigger]", conversation[-1]["content"])
                self.assertIn("[System Event: Will]", conversation[-2]["content"])
                if execute:
                    execute(runner)
                return text, [], []
        return Model()

    def test_real_send_restart_and_no_fake_user(self):
        ev = self.prepare()
        bot = QQThinking(self.store, self.client)
        bot.process_proactive(ev, self.model())
        bot.process_proactive(ev, Mock())
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual([m["role"] for m in self.store.context(self.store.identity(ev))], ["user", "assistant"])

    def test_new_input_and_group_activity_cancel_candidate(self):
        ev = self.prepare()
        self.insert(event("1", group="10", msg_id=502))
        self.assertEqual(guard(self.store, ev), "owner_active_elsewhere")
        self.insert(event("1", msg_id=503, text="等一下"))
        QQThinking(self.store, self.client).process_proactive(ev, Mock())
        self.assertFalse(self.api.calls)

    def test_gateway_allows_native_napcat_intent_and_cancels_later_write(self):
        ev = self.prepare("initiative")
        identity = self.store.identity(ev)
        with tool_context(ToolContext(identity, ev["event_id"], ev["event_id"])):
            result = self.client.raw_api("send_qzone_msg", {"content": "螃蟹小想法"})
            self.assertEqual(result["status"], "ok")
            self.insert(event("1", msg_id=504))
            with self.assertRaises(RuntimeError):
                self.client.raw_api("send_group_msg", {"group_id": "20", "message": "准备发的分享"})
        self.assertEqual(len(self.api.calls), 1)

    def test_no_contact_allows_reads_but_blocks_native_messages(self):
        ev = self.prepare("curiosity", contact=False)
        with tool_context(ToolContext(self.store.identity(ev), ev["event_id"], ev["event_id"])):
            self.assertEqual(self.client.raw_api("get_friend_list", {})["status"], "ok")
            with self.assertRaises(RuntimeError):
                self.client.raw_api("send_qzone_msg", {"content": "不能绕过静默"})

    def test_uncertain_write_never_restarts_model_or_saves_false_history(self):
        ev = self.prepare()
        self.api.send_status = "uncertain"
        bot = QQThinking(self.store, self.client)
        with self.assertRaises(RuntimeError):
            bot.process_proactive(ev, self.model())
        model = Mock()
        bot.process_proactive(ev, model)
        model.run_skill_protocol.assert_not_called()
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(len(self.store.context(self.store.identity(ev))), 1)
        with self.store.connection() as db:
            checkpoint = json.loads(db.execute("SELECT payload FROM turns").fetchone()[0])["activity"]
        self.assertEqual(checkpoint["mode"], "pause")
        self.assertEqual(checkpoint["delay_seconds"], 0)

    def test_crash_after_final_send_recovers_without_duplicate(self):
        ev = self.prepare()
        bot = QQThinking(self.store, self.client)
        identity = self.store.identity(ev)
        actions = bot.reply_actions(identity, ["一次发送"])
        self.store.begin_turn(ev["event_id"])
        self.store.save_turn(ev["event_id"], "ready", {"actions": actions, "next": 0})
        with tool_context(ToolContext(identity, ev["event_id"], ev["event_id"])):
            self.client.raw_api("send_private_msg", actions[0]["params"], operation_id=ev["event_id"]+":reply:0")
        bot.process_proactive(ev, Mock())
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(len(self.store.context(identity)), 2)

    def test_human_batch_priority_does_not_merge_internal_intent(self):
        self.prepare()
        self.insert(event("1", msg_id=505, text="新问题"))
        rows = self.store.claim("worker", 0, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["raw"])["post_type"], "message")

    def test_independent_search_runs_skill_without_sending_final_reflection(self):
        ev = self.prepare("curiosity", contact=False)
        self.runner_calls = []
        bot = QQThinking(self.store, self.client)
        with patch.object(bot.runner, "execute", side_effect=lambda *args, **kwargs: self.runner_calls.append(args) or "查到了原因"):
            bot.process_proactive(ev, self.model(lambda runner: runner.execute(
                {"skill": "web-research", "action": "browser_search", "args": {"query": "螃蟹为何横着走"}}, source="QQ")))
        self.assertEqual(len(self.runner_calls), 1)
        self.assertFalse(self.api.calls)
        with self.store.connection() as db:
            self.assertIn("查到了原因", db.execute("SELECT payload FROM turns").fetchone()[0])

    def test_native_uncertain_operation_is_persisted_and_not_regenerated(self):
        ev = self.prepare("initiative")
        bot = QQThinking(self.store, self.client)
        self.api.send_status = "uncertain"
        def execute(runner):
            self.client.raw_api("send_qzone_msg", {"content": "螃蟹小想法"})
        with self.assertRaises(RuntimeError):
            bot.process_proactive(ev, self.model(execute))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM proactive_runs").fetchone()[0], "uncertain")
        model = Mock()
        bot.process_proactive(ev, model)
        model.run_skill_protocol.assert_not_called()
        self.assertEqual(len(self.api.calls), 1)

    def test_scheduler_appraises_history_and_rechecks_slow_model(self):
        self.insert(event("1", msg_id=506, time=time.time()-1800))
        self.insert(event("2", msg_id=507, time=time.time()-1800))
        with self.store.connection() as db:
            db.execute("UPDATE events SET status='done'")
        now = [time.time()]
        memory = Mock()
        memory.activity_snapshot.return_value = {"state": {}, "last_owner_activity": None}
        memory.build_context.return_value = {"working": [], "recalled": [], "state": {}}
        appraisal = Mock(return_value={"emotion": {"valence": 1, "arousal": 1, "social": 1, "curiosity": 1},
                                      "interests": ["螃蟹"]})
        def decide(*_, **kwargs):
            self.insert(event("1", msg_id=509, text="模型思考时又说话了"))
            return intention()
        scheduler = QQProactive(self.store, memory, self.client, clock=lambda: now[0],
                                config=ProactiveConfig(interval=1, min_idle=0, startup_grace=0,
                                                       quiet_start=0, quiet_end=0),
                                decide=decide, appraise=appraisal)
        scheduler.tick()
        now[0] += 310
        scheduler.tick()
        appraisal.assert_called_once()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM proactive_runs").fetchone()[0], "skipped")
        self.assertEqual(len(scheduler.policies), 1)

    def test_owner_private_cooldown_does_not_block_public_or_other_private_targets(self):
        ev = self.prepare("initiative")
        ev["proactive"]["owner_contact_allowed"] = False
        with self.store.connection() as db:
            db.execute("UPDATE proactive_runs SET candidate=? WHERE id=?",
                       (json.dumps(ev["proactive"]), ev["event_id"]))
            db.execute("UPDATE events SET raw=? WHERE id=?", (json.dumps(ev), ev["event_id"]))
        with tool_context(ToolContext(self.store.identity(ev), ev["event_id"], ev["event_id"])):
            with self.assertRaisesRegex(RuntimeError, "主人私聊"):
                self.client.raw_api("send_private_msg", {"user_id": "1", "message": "不应打扰主人"})
            self.assertEqual(self.client.raw_api("send_qzone_msg", {"content": "公开发现"})["status"], "ok")
            self.assertEqual(self.client.raw_api("send_private_msg", {"user_id": "2", "message": "其他好友"})["status"], "ok")
        self.assertEqual(len(self.api.calls), 2)

    def test_resting_candidate_can_regulate_without_browser_or_qq_calls(self):
        ev = self.prepare("curiosity", contact=False)
        ev["proactive"].update(planning_only=True, owner_contact_allowed=False)
        with self.store.connection() as db:
            db.execute("UPDATE proactive_runs SET candidate=? WHERE id=?", (json.dumps(ev["proactive"]), ev["event_id"]))
            db.execute("UPDATE events SET raw=? WHERE id=?", (json.dumps(ev), ev["event_id"]))
        bot = QQThinking(self.store, self.client)
        snapshot = bot.memory.activity_snapshot()
        snapshot["life"] = {"phase": "resting"}
        def execute(runner):
            self.assertIn("未执行", runner.execute({"action": "website_open"}))
            runner.execute({"action": "regulate_life", "args": {"mode": "wake", "reason": "已经休息好了"}})
        with patch.object(bot.memory, "activity_snapshot", return_value=snapshot), patch.object(
            bot.runner, "execute", return_value='{"status":"ok","phase":"active"}') as actions:
            bot.process_proactive(ev, self.model(execute, text="重新考虑接下来做什么"))
        self.assertEqual(actions.call_count, 1)
        self.assertEqual(actions.call_args.args[0]["action"], "regulate_life")
        self.assertFalse(self.api.calls)

    def test_automatic_rest_can_be_reconsidered_through_real_memory_gateway(self):
        ev = self.prepare("curiosity", contact=False)
        ev["proactive"].update(planning_only=True, owner_contact_allowed=False)
        with self.store.connection() as db:
            db.execute("UPDATE proactive_runs SET candidate=? WHERE id=?", (json.dumps(ev["proactive"]), ev["event_id"]))
            db.execute("UPDATE events SET raw=? WHERE id=?", (json.dumps(ev), ev["event_id"]))
        ledger = self.memory_hub.service.life
        state = ledger._load(time.time())
        state.update(energy=.1, sleep_pressure=.9)
        ledger._save(state, time.time())
        self.assertEqual(ledger.snapshot()["phase"], "resting")
        bot = QQThinking(self.store, self.client)
        def execute(runner):
            from common.memory_journal import result_data
            result = result_data(runner.execute({"skill": "system-control", "action": "regulate_life", "args": {
                "mode": "wake", "reason": "主动重新考虑目前状态，短暂清醒后再取舍", "duration_minutes": 30}},
                source="QQ"))
            self.assertEqual(result["phase"], "active")
        with patch("chatBot.memory_hub.GatewayMemoryClient", return_value=bot.memory):
            bot.process_proactive(ev, self.model(execute, text="已重新调整状态"))
        bot.process_proactive(ev, Mock())  # Terminal turn cannot rerun regulation.
        self.assertEqual(ledger.snapshot()["phase"], "active")
        self.assertFalse(self.api.calls)


class ThinkingTurnTests(unittest.TestCase):
    def test_scheduler_starts_new_turn_and_preserves_reasoning_across_tool_rounds(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from openai.types.chat import ChatCompletionMessage
        from thinking_model_helper import ThinkingModelHelper
        from will_runtime import run_intention

        memory_context = {
            "envelope": {"channel_type": "qq_private", "scope": "owner", "owner_identity": True},
            "state": {},
            "working": [
                {"role": "user", "content": "先忙一会儿", "ts": time.time()-1800},
                {"role": "assistant", "content": "好呀", "ts": time.time()-1700},
            ],
        }
        responses = [
            ChatCompletionMessage(role="assistant", reasoning_content="先确认现在的时间",
                                  tool_calls=[{"id": "time", "type": "function", "function": {
                                      "name": "get_time", "arguments": "{}",
                                  }}]),
            ChatCompletionMessage(role="assistant", reasoning_content="再查一下可用的资料",
                                  tool_calls=[{"id": "search", "type": "function", "function": {
                                      "name": "browser_search", "arguments": '{"query":"螃蟹的步伐"}',
                                  }}]),
            ChatCompletionMessage(role="assistant", content="资料查到了，先记下来"),
        ]
        requests = []
        def complete(**kwargs):
            messages = deepcopy(kwargs["messages"])
            requests.append(messages)
            last_user = max(index for index, msg in enumerate(messages) if msg["role"] == "user")
            self.assertIn("[Internal Scheduler Trigger]", messages[last_user]["content"])
            # Mirrors DeepSeek's current-turn validation: system messages alone
            # do not separate ordinary historical replies from a thinking turn.
            current_assistants = [msg for msg in messages[last_user+1:] if msg["role"] == "assistant"]
            for msg in current_assistants:
                self.assertTrue(msg.get("reasoning_content"))
                self.assertTrue(msg.get("tool_calls"))
            return SimpleNamespace(choices=[SimpleNamespace(message=responses.pop(0))])

        model = ThinkingModelHelper.__new__(ThinkingModelHelper)
        model.model = "test"
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        runner = Mock()
        runner.instructions_for_source.return_value = "测试工具目录"
        runner.function_tools.return_value = []
        runner.action_to_skill = {"get_time": "system-control", "browser_search": "web-research"}
        runner.execute.return_value = "读取成功"
        with tool_context(ToolContext(LOCAL_IDENTITY, autonomous=True)), patch(
            "prompt_builder.conversation_style_guard", return_value="--- 本轮角色口吻\n保持当前口吻",
        ):
            text, outcomes = run_intention(model, runner, memory_context, {
                **intention("curiosity"), "contact_allowed": False,
            }, lambda: "")
        self.assertEqual(text, "资料查到了，先记下来")
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(requests), 3)
        assistants = [msg for msg in requests[-1] if msg.get("tool_calls")]
        self.assertEqual([msg["reasoning_content"] for msg in assistants],
                         ["先确认现在的时间", "再查一下可用的资料"])
        self.assertEqual([msg["tool_call_id"] for msg in requests[-1] if msg["role"] == "tool"],
                         ["time", "search"])
        self.assertTrue(any(msg["role"] == "system"
                            and msg["content"].startswith("<historical_assistant_message>")
                            and "好呀" in msg["content"] for msg in requests[0]))
        self.assertFalse(any(msg["role"] == "assistant" for msg in requests[0]))


if __name__ == "__main__":
    unittest.main()
