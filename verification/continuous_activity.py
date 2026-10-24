"""Continuous autonomy regression with simulated tools; no external posts."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from common.activity import activity_checkpoint, consume_desktop_handoff, save_desktop_handoff
from common.proactive import ProactiveConfig, ProactivePolicy
from tools.context import ToolContext, tool_context
from chatBot.messages import LOCAL_IDENTITY
from thingking.src.will_runtime import WillSkillRunner, run_intention, ActivityYield
from tools.activity_tool import activity_continue, activity_finish, activity_pause, autonomy_control
from verification.proactive_behavior import intention
from verification import proactive_behavior as fixtures
from verification.napcat_integration import event
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

    def test_checkpoint_survives_restart_and_sleep_allows_throttled_planning(self):
        checkpoint = activity_checkpoint(self.candidate, {"mode": "continue"}, [])
        self.policy.reserve(self.candidate)
        self.policy.resolve(self.now, delivered=True, activity=checkpoint)
        restored = ProactivePolicy(self.now + 3, self.config, self.policy.dump())
        self.assertTrue(restored.tick(self.now + 4, self.user, life={"phase": "resting"}))
        self.assertTrue(restored.state.planning_only)
        self.assertTrue(restored.tick(self.now + 5, self.user, life={"phase": "active"}))
        self.assertEqual(restored.state.activity["intent"], self.candidate["intent"])

    def test_desktop_owner_handoff_is_acknowledged_in_saved_policy_and_not_reapplied(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "common.activity.control_path", return_value=Path(temp) / "control.json",
        ):
            checkpoint = activity_checkpoint(self.candidate, {"mode": "continue"}, [])
            checkpoint.update(kind="curiosity", topic="螃蟹", reason="兴趣", next_at=self.now)
            save_desktop_handoff("desktop:owner", checkpoint)
            self.assertTrue(consume_desktop_handoff(self.policy))
            restored = ProactivePolicy(self.now + 3, self.config, self.policy.dump())
            self.assertFalse(consume_desktop_handoff(restored))
            restored.state.activity["mode"] = "finish"
            self.assertFalse(consume_desktop_handoff(restored))
            self.assertEqual(restored.state.activity["mode"], "finish")

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

    def test_slow_first_model_response_still_allows_one_real_action_before_yield(self):
        runner = Mock()
        runner.execute.return_value = '{"status":"ok"}'
        wrapper = WillSkillRunner(runner, {"contact_allowed": True}, lambda: "")
        wrapper.started -= 1000
        with tool_context(ToolContext(LOCAL_IDENTITY, autonomous=True)):
            wrapper.execute({"action": "website_scroll", "args": {}})
            with self.assertRaises(ActivityYield):
                wrapper.execute({"action": "website_scroll", "args": {}})
        self.assertEqual(runner.execute.call_count, 1)

    def test_verification_pauses_before_another_tool_call(self):
        runner = Mock()
        runner.execute.return_value = json.dumps({"status": "blocked_human", "human_action_required": True})
        wrapper = WillSkillRunner(runner, {"contact_allowed": True}, lambda: "")
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        with tool_context(context):
            with self.assertRaises(ActivityYield):
                wrapper.execute({"action": "website_interact", "args": {}})
        self.assertEqual(context.activity["delay_seconds"], 0)
        self.assertEqual(runner.execute.call_count, 1)

    def test_success_and_no_action_both_leave_the_next_choice_to_the_model(self):
        candidate = {**intention("curiosity"), "activity": {"idle_batches": 2}}
        runner = Mock()
        runner.execute.return_value = '{"status":"ok","page_state":{"scroll_y":800}}'

        class Model:
            def run_skill_protocol(inner, conversation, guarded, **kwargs):
                guarded.execute({"action": "website_scroll", "args": {}})
                return "这一屏看完了", [], []

        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        with tool_context(context):
            run_intention(Model(), runner, {}, candidate, lambda: "")
        self.assertEqual(context.activity["mode"], "review")
        self.assertEqual(context.activity["decision_source"], "runtime")
        model = Mock()
        model.run_skill_protocol.return_value = ("暂时没做", [], [])
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        with tool_context(context):
            run_intention(model, runner, {}, candidate, lambda: "")
        self.assertEqual(context.activity["mode"], "review")
        self.assertEqual(context.activity["delay_seconds"], 2)

    def test_explicit_model_continue_is_respected_even_without_a_page_action(self):
        from common.activity import complete_activity
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        context.activity.update(mode="continue", next_intent="继续浏览", delay_seconds=2)
        complete_activity(context, intention("curiosity"), [{
            "action": "activity_continue", "status": "succeeded", "result": "ok",
        }])
        self.assertEqual(context.activity["mode"], "continue")
        self.assertEqual(context.activity["delay_seconds"], 2)

    def test_model_can_finish_after_success_without_dispatching_another_action(self):
        candidate = {**intention("curiosity"), "activity": {
            "mode": "review", "observations": [{"action": "website_watch", "result": "已看到海鸟捕食"}],
        }}
        decision = {"mode": "finish", "reason": "这一小段已经满足好奇，先停下来",
                    "topic": "海鸟捕食", "next_intent": "", "interest_level": 0.2, "delay_seconds": 0}
        model, runner = Mock(), Mock()
        model.complete_text.return_value = json.dumps(decision)
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        with tool_context(context):
            text, outcomes = run_intention(model, runner, {}, candidate, lambda: "")
        self.assertEqual(context.activity["mode"], "finish")
        self.assertIn("满足好奇", text)
        self.assertEqual(outcomes, [])
        model.run_skill_protocol.assert_not_called()
        runner.execute.assert_not_called()
        self.assertIn("海鸟捕食", str(model.complete_text.call_args))

    def test_model_changes_topic_after_no_action_and_chooses_its_own_cadence(self):
        candidate = {**intention("curiosity"), "activity": {"mode": "review", "observations": []}}
        decision = {"mode": "continue", "reason": "比起继续翻页，我现在更想看看海鸟",
                    "topic": "海鸟行为", "next_intent": "搜索海鸟如何捕食", "interest_level": 0.8,
                    "delay_seconds": 35}
        model, runner = Mock(), Mock()
        model.complete_text.return_value = json.dumps(decision)
        model.run_skill_protocol.return_value = ("先看看新的方向", [], [])
        original = json.dumps(candidate)
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        with tool_context(context):
            run_intention(model, runner, {}, candidate, lambda: "")
        self.assertEqual(context.activity["topic"], "海鸟行为")
        self.assertEqual(context.activity["delay_seconds"], 35)
        self.assertEqual(context.activity["mode"], "review")
        self.assertIn("搜索海鸟如何捕食", str(model.run_skill_protocol.call_args))
        self.assertEqual(json.dumps(candidate), original)  # Signed event remains untouched.

    def test_model_pause_has_no_actions_and_invalid_choice_cannot_dispatch(self):
        candidate = {**intention("curiosity"), "activity": {"mode": "review"}}
        model, runner = Mock(), Mock()
        model.complete_text.return_value = json.dumps({
            "mode": "pause", "reason": "等这一段播放一会再回来观察", "next_intent": "",
            "topic": "自然视频", "delay_seconds": 47, "interest_level": 0.6,
        })
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        with tool_context(context):
            run_intention(model, runner, {}, candidate, lambda: "")
        self.assertEqual(context.activity["mode"], "pause")
        self.assertEqual(context.activity["delay_seconds"], 47)
        model.run_skill_protocol.assert_not_called()
        model.complete_text.return_value = '{"mode":"continue","delay_seconds":-1}'
        with tool_context(ToolContext(LOCAL_IDENTITY, autonomous=True)):
            with self.assertRaises(ValueError):
                run_intention(model, runner, {}, candidate, lambda: "")
        runner.execute.assert_not_called()

    def test_model_selected_pause_resumes_as_review_at_selected_time(self):
        now = time.time()
        config = ProactiveConfig(min_idle=0, startup_grace=0, quiet_start=0, quiet_end=0)
        policy = ProactivePolicy(now, config)
        user = {"id": 1, "ts": now - 100, "content": "随便你刷"}
        policy.tick(now, user)
        candidate = {**intention("curiosity"), "based_on_user": "1", "id": "pause"}
        policy.reserve(candidate)
        checkpoint = activity_checkpoint(candidate, {
            "mode": "pause", "delay_seconds": 47, "decision_source": "model",
        }, [])
        policy.resolve(now, delivered=True, activity=checkpoint)
        self.assertFalse(policy.tick(now + 46, user))
        self.assertTrue(policy.tick(now + 48, user))
        self.assertEqual(policy.state.activity["mode"], "review")

    def test_desktop_control_ends_stream_without_an_extra_request_or_spoken_receipt(self):
        from thingking.src.thinking_stream_runner import run_main_agent_rounds
        from will_runtime import WillSkillRunner as DesktopRunner
        command = {"skill": "activity", "action": "activity_continue",
                   "args": {"next_intent": "继续浏览"}}
        runner = Mock()
        runner.parse_command.return_value = command
        runner.execute.side_effect = lambda *_args, **_kwargs: json.dumps(activity_continue("继续浏览"))
        wrapper = DesktopRunner(runner, intention("initiative"), lambda: "", ordinary=True)
        main = Mock()
        main.run_one_turn_streaming.return_value = (
            iter(["[[SKILL_CALL]]" + json.dumps(command) + "[[/SKILL_CALL]]"]), {},
        )
        send = Mock()
        with tool_context(ToolContext(LOCAL_IDENTITY, "event", "turn", user_text="持续浏览")):
            result = run_main_agent_rounds(
                messages=[], main_agent=main, skill_runner=wrapper, zmq_send=send,
                is_only_action_or_empty=lambda _: False, enable_audio=True, source="ASR",
                interrupt_requested=lambda: False,
            )
        self.assertEqual(result["full_raw_response"], "")
        self.assertEqual(main.run_one_turn_streaming.call_count, 1)
        send.assert_not_called()

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
                with tool_context(ToolContext(LOCAL_IDENTITY, user_text="暂停自主活动")) as context:
                    self.assertEqual(autonomy_control("pause")["control"]["mode"], "pause")
                    self.assertEqual(context.activity["mode"], "pause")
                    self.assertEqual(context.activity["delay_seconds"], 0)
                with tool_context(ToolContext(LOCAL_IDENTITY, user_text="停止自主活动")) as context:
                    self.assertEqual(autonomy_control("stop")["control"]["mode"], "stop")
                    self.assertEqual(context.activity["mode"], "finish")
                with tool_context(ToolContext(LOCAL_IDENTITY, user_text="不要恢复自主活动")):
                    with self.assertRaisesRegex(PermissionError, "否定"):
                        autonomy_control("resume")


class QQContinuousTests(unittest.TestCase):
    setUp = fixtures.QQTests.setUp
    tearDown = fixtures.QQTests.tearDown
    insert = fixtures.QQTests.insert
    prepare = fixtures.QQTests.prepare

    def test_owner_chat_hands_off_three_batches_and_survives_scheduler_restart(self):
        self.insert(event("1", msg_id=700, text="那你刷去吧，随便你刷"))
        rows = self.store.claim("worker", 0, 0)
        bot = QQThinking(self.store, self.client)
        scrolls = []

        def website(route, payload, **kwargs):
            self.assertEqual(route, "/v1/scroll")
            scrolls.append(len(scrolls) + 1)
            return json.dumps({"status": "ok", "page_state": {"scroll_y": 600 * len(scrolls)}})

        class OwnerModel:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                runner.execute({"skill": "website", "action": "website_scroll",
                                "args": {"platform": "bilibili"}}, source="QQ")
                # An ordinary final sentence must still hand off this open-ended request.
                return "这次真的往下翻了一屏", [], []

        class BatchModel:
            def complete_text(inner, messages, **kwargs):
                return json.dumps({
                    "mode": "continue", "topic": "首页推荐", "next_intent": "看看下一屏有哪些内容",
                    "reason": "推荐还有想探索的内容", "interest_level": 0.7, "delay_seconds": 2,
                })

            def run_skill_protocol(inner, conversation, runner, **kwargs):
                self.assertIn("scroll_y", str(conversation))
                runner.execute({"skill": "website", "action": "website_scroll",
                                "args": {"platform": "bilibili"}}, source="QQ")
                return "继续看下一屏", [], []

        now = time.time()
        config = ProactiveConfig(quiet_start=0, quiet_end=0)  # Keep real startup/idle defaults.
        with patch("tools.website_tool._request", side_effect=website):
            bot.process(rows, OwnerModel())
            bot.process(rows, Mock())  # Done turn: no replay.
            self.store.finish([row["id"] for row in rows], "worker")
            self.memory_hub.sync()
            for batch in range(3):
                now += 3
                scheduler = QQProactive(self.store, bot.memory, self.client, config=config,
                                       clock=lambda: now, appraise=Mock(side_effect=AssertionError("appraisal")))
                scheduler.tick()
                scheduler.tick()  # First observation may update user_ref.
                with self.store.connection() as db:
                    pending = db.execute("SELECT * FROM events WHERE status='pending'").fetchall()
                self.assertEqual(len(pending), 1)
                self.assertEqual(json.loads(pending[0]["raw"])["post_type"], "bines_proactive")
                with patch("chatBot.store.time.time", return_value=now):
                    claimed = self.store.claim("worker", 0, 0)
                    self.assertTrue(claimed)
                    bot.process(claimed, BatchModel())
                    self.store.finish([row["id"] for row in claimed], "worker")
        self.assertEqual(len(scrolls), 4)
        self.assertEqual(sum(action in {"send_private_msg", "send_msg"} for action, _ in self.api.calls), 1)
        self.assertEqual(len([row for row in self.store.context(self.store.identity(event("1")))
                              if row["role"] == "user"]), 1)

    def test_new_owner_finish_handoff_waits_for_pending_then_overrides_old_progress(self):
        ev = self.prepare("curiosity")
        policy = ProactivePolicy(time.time(), ProactiveConfig(startup_grace=0))
        policy.reserve(ev["proactive"])
        bot = QQThinking(self.store, self.client)
        bot.process_proactive(ev, Mock(**{"run_skill_protocol.return_value": ("", [], [])}))
        scheduler = QQProactive(self.store, self.memory_hub, self.client)
        conversation = self.store.identity(ev).conversation
        self.store.begin_turn("owner:stop")
        self.store.save_turn("owner:stop", "done", {"activity": {"mode": "finish"}}, handoff={
            "conversation": conversation, "checkpoint": {"mode": "finish", "next_at": time.time()},
        })
        scheduler.consume_handoffs(conversation, policy, time.time())
        self.assertTrue(policy.state.pending)
        scheduler.recover(policy, time.time())
        scheduler.consume_handoffs(conversation, policy, time.time())
        self.assertEqual(policy.state.activity, {})
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT consumed FROM activity_handoffs").fetchone()[0], 1)

    def test_owner_interrupt_sees_latest_finished_batch_before_scheduler_recovers_it(self):
        ev = self.prepare("curiosity")
        conversation = self.store.identity(ev).conversation
        policy = ProactivePolicy(time.time())
        policy.reserve(ev["proactive"])
        scheduler = QQProactive(self.store, self.memory_hub, self.client)
        scheduler.save(conversation, policy)
        self.store.begin_turn(ev["event_id"])
        self.store.save_turn(ev["event_id"], "done", {"activity": {
            "mode": "continue", "intent": "刚看完第二页，下一步看第三页", "sequence": 2,
        }})
        self.assertEqual(self.store.current_activity(conversation)["sequence"], 2)

    def test_rejected_browser_ref_does_not_block_confirmed_qq_send_or_handoff(self):
        from tools.website_tool import WebsiteRequestError
        self.insert(event("1", msg_id=702, text="继续浏览B站"))
        rows = self.store.claim("worker", 0, 0)
        bot = QQThinking(self.store, self.client)

        class Model:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                result = runner.execute({"skill": "website", "action": "website_interact",
                                         "args": {"platform": "bilibili", "action": "fill",
                                                  "ref": "@e1", "value": "螃蟹"}}, source="QQ")
                self.assertEqual(json.loads(result)["status"], "failed")
                runner.execute({"skill": "napcat", "action": "napcat_call",
                                "args": {"action": "send_private_msg",
                                         "params": {"user_id": "1", "message": "重新观察后再继续"}}}, source="QQ")
                runner.execute({"skill": "activity", "action": "activity_continue",
                                "args": {"next_intent": "刷新快照后继续浏览"}}, source="QQ")
                self.fail("activity control must yield immediately")

        with patch("tools.website_tool._request", side_effect=WebsiteRequestError("元素含义已变化", rejected=True)):
            with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
                bot.process(rows, Model())
        self.assertEqual(sum(action == "send_private_msg" for action, _ in self.api.calls), 1)
        self.assertEqual(self.store.current_activity(self.store.identity(event("1")).conversation)["mode"], "continue")

    def test_crash_after_journaled_browser_action_keeps_goal_paused_without_model_replay(self):
        from common.memory_journal import MemoryJournal
        from chatBot.memory_hub import identity_envelope
        self.insert(event("1", msg_id=703, text="持续浏览B站"))
        rows = self.store.claim("worker", 0, 0)
        bot = QQThinking(self.store, self.client)
        import hashlib
        turn = rows[0].get("batch") or hashlib.sha256("|".join(row["id"] for row in rows).encode()).hexdigest()
        identity = self.store.identity(event("1"))
        self.store.begin_turn(turn)
        journal = MemoryJournal(bot.outbox, identity_envelope(identity), turn)
        journal.begin({"action": "website_interact", "args": {"action": "click"}}, category="tool")
        model = Mock()
        with self.assertRaisesRegex(RuntimeError, "禁止重新生成"):
            bot.process(rows, model)
        model.run_skill_protocol.assert_not_called()
        self.assertEqual(self.store.current_activity(identity.conversation)["mode"], "pause")

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
