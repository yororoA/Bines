"""Willingness and fake-NapCat regression; no real outbound calls."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]
from common.proactive import (
    ProactiveConfig, ProactivePolicy, context_blocked, human_ref, parse_appraisal, parse_decision,
)
from chatBot.proactive import QQProactive, guard
from verification.napcat_integration import Integration, event
from thingking.qq_thinking import QQThinking
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
        self.assertIsNone(parse_decision(json.dumps({"should_act": True, **intention()}), ["螃蟹 横着走！"]))
        self.assertIsNone(parse_appraisal('{"emotion":{"social":"高"}}'))
        self.assertIsNotNone(parse_appraisal(json.dumps({"emotion": {
            "valence": 0.1, "arousal": 0.2, "social": 0.3, "curiosity": 0.4}, "interests": ["螃蟹"]})))

    def test_uncertain_action_is_not_automatically_repeated(self):
        self.policy.reserve(self.candidate("initiative"))
        self.policy.resolve(self.now, uncertain=True)
        self.assertIn(self.candidate()["topic"], self.policy.state.topics)
        self.assertEqual(self.policy.state.recent_outcomes[-1]["outcome"], "uncertain")


class WillToolsTests(unittest.TestCase):
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
    setUp = Integration.setUp
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
                                config=ProactiveConfig(interval=1, min_idle=0, startup_grace=0),
                                decide=decide, appraise=appraisal)
        scheduler.tick()
        now[0] += 310
        scheduler.tick()
        appraisal.assert_called_once()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM proactive_runs").fetchone()[0], "skipped")
        self.assertEqual(len(scheduler.policies), 1)


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
        with patch("prompt_builder.conversation_style_guard", return_value="--- 本轮角色口吻\n保持当前口吻"):
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
        self.assertTrue(any(msg["role"] == "assistant" and msg["content"] == "好呀"
                            and "reasoning_content" not in msg for msg in requests[0]))


if __name__ == "__main__":
    unittest.main()
