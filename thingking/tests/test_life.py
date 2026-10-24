"""Persistent life continuity across time, scopes and restart."""
from datetime import datetime
import json
import unittest
from unittest.mock import Mock, patch

from helpers import MemoryFixture, OWNER_QQ, STRANGER_QQ, local_env, qq_group_env, qq_private_env
from memory.models import ZONE
from memory.life import LifeConfig, LifeLedger, _energy_delta, _rest_recovery
from common.proactive import ProactiveConfig, ProactivePolicy, compact_life
from chatBot.messages import LOCAL_IDENTITY, Identity
from tools.context import ToolContext, tool_context
import prompt_builder


def stamp(day=22, hour=12, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp()


class LifeLedgerTests(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture(clock_start=stamp())

    def tearDown(self):
        self.fx.close()

    def test_birth_uses_earliest_real_record_and_is_stable_on_restart(self):
        earlier = stamp(20, 9)
        self.fx.service.record_turn(local_env(earlier), "第一次有记录的交流", "我在")
        first = self.fx.service.life_snapshot()
        count = self.fx.service.life.event_count()
        restarted = LifeLedger(self.fx.repo, self.fx.clock)
        second = restarted.snapshot()
        self.assertEqual(first["born_at"], earlier)
        self.assertEqual(second["born_at"], earlier)
        self.assertEqual(restarted.event_count(), count)
        self.assertEqual(second["day_number"], 3)

    def test_circadian_progresses_sleep_wake_energy_and_pressure(self):
        noon = self.fx.service.life_snapshot()
        self.assertEqual(noon["phase"], "active")
        self.fx.clock.now = stamp(23, 2)
        asleep = self.fx.service.life_snapshot()
        self.assertEqual(asleep["phase"], "sleeping")
        self.assertGreater(asleep["sleep_pressure"], noon["sleep_pressure"])
        self.fx.clock.now = stamp(23, 9)
        waking = self.fx.service.life_snapshot()
        self.assertEqual(waking["phase"], "waking")
        self.assertGreater(waking["energy"], asleep["energy"])
        self.assertGreaterEqual(waking["wake_cycles"], 2)
        self.assertGreater(waking["last_wake_at"], asleep["last_sleep_at"])

    def test_real_human_message_wakes_night_without_faking_activity(self):
        self.fx.clock.now = stamp(23, 2)
        sleeping = self.fx.service.life_snapshot()
        self.assertEqual(sleeping["phase"], "sleeping")
        self.fx.service.record_message(local_env(self.fx.clock()), "user", "半夜醒了吗",
                                       source_ref="night-message")
        awake = self.fx.service.life_snapshot()
        self.assertEqual(awake["phase"], "night_awake")
        self.assertEqual(awake["today"]["human_turns"], 1)
        self.fx.service.record_message(local_env(self.fx.clock()), "assistant", "醒了",
                                       source_ref="night-reply", state_update=False)
        self.assertEqual(self.fx.service.life_snapshot()["today"]["assistant_turns"], 1)

    def test_autonomous_goal_lifecycle_is_idempotent_and_scoped(self):
        envelope = local_env(self.fx.clock())
        base = {
            "category": "turn", "action": "autonomous_turn", "autonomous": True,
            "turn_id": "will:stars", "candidate": {
                "kind": "curiosity", "topic": "辨认今晚的星座", "intent": "查找今晚可见星座",
            },
            "started_at": self.fx.clock(), "status": "started", "result": "",
        }
        created = self.fx.service.record_experience(envelope, "life-goal", base, revision=1)
        active = self.fx.service.life_snapshot()
        self.assertEqual(active["current_focus"], "辨认今晚的星座")
        self.assertEqual(active["goals"][0]["status"], "active")
        self.fx.clock.advance(60)
        done = {**base, "finished_at": self.fx.clock(), "status": "completed", "result": "查完了"}
        self.fx.service.record_experience(envelope, "life-goal", done, revision=2)
        self.fx.service.record_experience(envelope, "life-goal", done, revision=2)
        finished = self.fx.service.life_snapshot()
        self.assertEqual(len(finished["goals"]), 1)
        self.assertEqual(finished["goals"][0]["status"], "completed")
        self.assertEqual(finished["current_focus"], "")
        public = self.fx.service.life_snapshot("qq:test:group:1")
        self.assertNotIn("goals", public)
        self.assertNotIn("milestones", public)
        self.assertNotIn("辨认今晚的星座", json.dumps(public, ensure_ascii=False))
        self.fx.service.delete_memory(created["id"])
        self.assertEqual(self.fx.service.life_snapshot()["goals"], [])

    def test_existing_experiences_are_backfilled_once(self):
        record = {
            "category": "tool", "action": "browser_search", "autonomous": True,
            "turn_id": "old-will", "candidate": {"topic": "旧日月相", "kind": "curiosity"},
            "started_at": self.fx.clock(), "finished_at": self.fx.clock(),
            "status": "succeeded", "result": "查到资料",
        }
        self.fx.service.record_experience(local_env(self.fx.clock()), "old-experience", record, revision=1)
        count = self.fx.service.life.event_count()
        LifeLedger(self.fx.repo, self.fx.clock)
        LifeLedger(self.fx.repo, self.fx.clock)
        self.assertEqual(self.fx.service.life.event_count(), count)
        self.assertEqual(self.fx.service.life_snapshot()["goals"][0]["title"], "旧日月相")
        self.fx.clock.advance(7 * 3600)
        self.assertEqual(self.fx.service.life_snapshot()["goals"][0]["status"], "completed")

    def test_recall_updates_life_event_without_counting_as_human(self):
        self.fx.service.record_message(local_env(self.fx.clock()), "user", "临时消息",
                                       source_ref="recall-life")
        before = self.fx.service.life_snapshot()["today"]["human_turns"]
        self.fx.service.record_message(local_env(self.fx.clock()), "user", "[消息已撤回]",
                                       source_ref="recall-life", recalled=True)
        after = self.fx.service.life_snapshot()["today"]["human_turns"]
        self.assertEqual(before, 1)
        self.assertEqual(after, 0)
        self.assertEqual(self.fx.service.life_snapshot()["last_human_at"], 0)

    def test_context_prompt_and_export_include_verifiable_life(self):
        self.fx.service.record_turn(local_env(self.fx.clock()), "今天怎么样", "还在运行")
        context = self.fx.service.build_context(local_env(self.fx.clock()), "生命")
        self.assertIn("life", context)
        text = "\n".join(item["content"] for item in prompt_builder.build_conversation_messages(context)
                         if item["role"] == "system")
        self.assertIn("--- 生命连续性 ---", text)
        self.assertIn("连续生命记录", text)
        day = self.fx.service.life.day_summary("2026-10-22")
        self.assertEqual(day["human_turns"], 1)
        self.assertEqual(day["assistant_turns"], 1)
        exported = self.fx.service.export()
        self.assertEqual(exported["life"]["born_at"], context["life"]["born_at"])
        self.assertEqual(self.fx.service.stats()["life_events"], self.fx.service.life.event_count())

    def test_nonowner_context_has_public_life_without_private_focus(self):
        record = {
            "category": "turn", "action": "autonomous_turn", "autonomous": True,
            "turn_id": "private-focus", "candidate": {"topic": "管理员的私人目标", "kind": "initiative"},
            "started_at": self.fx.clock(), "status": "started", "result": "",
        }
        self.fx.service.record_experience(local_env(self.fx.clock()), "private-focus", record, revision=1)
        self.fx.service.record_message(local_env(self.fx.clock()), "user", "只属于管理员的交流",
                                       source_ref="owner-life-private")
        context = self.fx.service.build_context(qq_private_env(STRANGER_QQ), "")
        self.assertNotIn("goals", context["life"])
        text = str(prompt_builder.build_conversation_messages(context))
        self.assertIn("生命连续性", text)
        self.assertNotIn("管理员的私人目标", text)
        self.assertEqual(context["life"]["today"]["human_turns"], 0)

    def test_bounded_rest_is_idempotent_persistent_and_expires(self):
        before_events = self.fx.service.life.event_count()
        result = self.fx.service.regulate_life(
            "owner", "rest", 60, "连续处理任务后主动休息", "rest-once"
        )
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["life"]["phase"], "resting")
        self.assertEqual(result["life"]["regulation"]["mode"], "rest")
        context = self.fx.service.build_context(local_env(self.fx.clock()), "")
        prompt = str(prompt_builder.build_conversation_messages(context))
        self.assertIn("连续处理任务后主动休息", prompt)
        until = result["life"]["regulation"]["until"]
        duplicate = self.fx.service.regulate_life(
            "owner", "rest", 120, "这次重试不应延长休息", "rest-once"
        )
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["life"]["regulation"]["until"], until)
        self.assertEqual(self.fx.service.life.event_count(), before_events + 1)
        restarted = LifeLedger(self.fx.repo, self.fx.clock)
        self.assertEqual(restarted.snapshot()["phase"], "resting")
        self.fx.clock.advance(61 * 60)
        self.assertNotEqual(restarted.snapshot()["phase"], "resting")

    def test_real_message_wakes_self_chosen_rest(self):
        self.fx.service.regulate_life(
            "owner", "rest", 60, "先安静休息一小时", "rest-before-message"
        )
        self.fx.clock.advance(1)
        self.fx.service.record_message(
            local_env(self.fx.clock()), "user", "现在醒醒", source_ref="wake-message"
        )
        life = self.fx.service.life_snapshot()
        self.assertEqual(life["phase"], "active")
        self.assertEqual(life["regulation"], {})

    def test_wake_override_can_cross_sleep_window_then_returns_to_schedule(self):
        self.fx.clock.now = stamp(23, 2)
        self.assertEqual(self.fx.service.life_snapshot()["phase"], "sleeping")
        awake = self.fx.service.regulate_life(
            "owner", "wake", 30, "需要完成一项短暂的夜间检查", "night-wake"
        )
        self.assertEqual(awake["life"]["phase"], "active")
        self.fx.clock.advance(31 * 60)
        self.assertEqual(self.fx.service.life_snapshot()["phase"], "sleeping")

    def test_auto_rest_triggers_from_real_thresholds(self):
        row = self.fx.repo.conn.execute("SELECT data FROM life_state WHERE id='bines'").fetchone()
        state = json.loads(row["data"])
        state.update(energy=0.1, sleep_pressure=0.95, last_tick=self.fx.clock())
        self.fx.repo.conn.execute(
            "UPDATE life_state SET data=? WHERE id='bines'",
            (json.dumps(state, ensure_ascii=False),),
        )
        life = self.fx.service.life_snapshot()
        self.assertEqual(life["phase"], "resting")
        self.assertEqual(life["regulation"]["source"], "automatic")
        self.assertIn("自动", life["regulation"]["reason"])

    def test_internal_records_do_not_charge_energy_or_reset_zero(self):
        before = self.fx.service.life_snapshot()
        for index, category in enumerate(("feedback", "decision", "observation", "tool") * 10):
            self.fx.service.life.record_experience("owner", f"internal:{index}", {
                "category": category, "action": "autonomous_feedback",
                "status": "completed", "finished_at": self.fx.clock(),
            }, 1)
        after = self.fx.service.life_snapshot()
        self.assertEqual(after["energy"], before["energy"])
        self.assertEqual(after["sleep_pressure"], before["sleep_pressure"])
        row = self.fx.repo.conn.execute("SELECT data FROM life_state WHERE id='bines'").fetchone()
        state = json.loads(row["data"])
        state.update(energy=0, sleep_pressure=0, last_tick=self.fx.clock())
        self.fx.repo.conn.execute("UPDATE life_state SET data=? WHERE id='bines'", (json.dumps(state),))
        self.fx.service.life.record_experience("owner", "zero-feedback", {
            "category": "feedback", "status": "completed", "finished_at": self.fx.clock(),
        }, 1)
        self.assertEqual(self.fx.service.life_snapshot()["energy"], 0)
        self.assertEqual(self.fx.service.life_snapshot()["sleep_pressure"], 0)

    def test_work_time_is_charged_once_across_receipts_revisions_and_restart(self):
        before = self.fx.service.life_snapshot()
        record = {
            "category": "turn", "turn_id": "one-work-turn", "status": "completed",
            "duration_seconds": 900, "finished_at": self.fx.clock(),
        }
        ledger = self.fx.service.life
        ledger.record_experience("owner", "work:one", record, 1)
        ledger.record_experience("owner", "work:one", record, 2)
        ledger.record_experience("owner", "work:other-receipt", record, 1)
        after = self.fx.service.life_snapshot()
        self.assertAlmostEqual(after["energy"], before["energy"] - 0.0025, places=4)
        self.assertAlmostEqual(after["sleep_pressure"], before["sleep_pressure"] + 0.00375, places=3)
        restarted = LifeLedger(self.fx.repo, self.fx.clock)
        restarted.record_experience("owner", "work:one", {**record, "duration_seconds": 1800}, 3)
        self.assertAlmostEqual(restarted.snapshot()["energy"], before["energy"] - 0.005, places=4)

    def test_one_to_two_hour_nap_restores_without_resetting_counters(self):
        one_hour = _rest_recovery(.1, .95, 3600)
        ninety_minutes = _rest_recovery(.1, .95, 5400)
        two_hours = _rest_recovery(.1, .95, 7200)
        self.assertGreater(one_hour[0], .65)
        self.assertLess(one_hour[1], .4)
        self.assertGreater(ninety_minutes[0], .75)
        self.assertLess(ninety_minutes[1], .25)
        self.assertGreater(two_hours[0], .84)
        self.assertTrue(.12 < two_hours[1] < .2)
        self.assertLess(two_hours[0], .92)
        self.assertEqual(_rest_recovery(1, 0, 7200), (1, 0))
        self.assertEqual(_rest_recovery(.4, .7, 0), (.4, .7))

    def test_nap_expiry_recovery_is_independent_of_observation_frequency(self):
        other = MemoryFixture(clock_start=self.fx.clock())
        self.addCleanup(other.close)
        for fx in (self.fx, other):
            state = fx.service.life._load(fx.clock())
            state.update(energy=.1, sleep_pressure=.95, last_tick=fx.clock())
            fx.service.life._save(state, fx.clock())
            fx.service.regulate_life("owner", "rest", 90, "一次完整的午睡", "nap-boundary")
        self.fx.clock.advance(2 * 3600)
        delayed = self.fx.service.life_snapshot()
        for _ in range(6):
            other.clock.advance(20 * 60)
            observed = other.service.life_snapshot()
        self.assertEqual(delayed["phase"], "active")
        self.assertAlmostEqual(delayed["energy"], observed["energy"], places=4)
        self.assertAlmostEqual(delayed["sleep_pressure"], observed["sleep_pressure"], places=4)
        recovered_energy, recovered_pressure = _rest_recovery(.1, .95, 5400)
        self.assertAlmostEqual(delayed["energy"], recovered_energy - .006, places=4)
        self.assertAlmostEqual(delayed["sleep_pressure"], recovered_pressure + .0175, places=4)

    def test_six_hours_idle_has_small_energy_cost(self):
        energy_cost, _ = _energy_delta("active", 6 * 3600)
        self.assertAlmostEqual(energy_cost, -.072)
        self.assertGreater(.8 + energy_cost, .7)

    def test_four_hours_full_work_does_not_exhaust_energy(self):
        before = self.fx.service.life_snapshot()["energy"]
        for batch in range(4):
            self.fx.clock.advance(3600)
            self.fx.service.life.record_experience("owner", f"long-work:{batch}", {
                "category": "turn", "turn_id": f"long-work:{batch}", "status": "completed",
                "duration_seconds": 3600, "finished_at": self.fx.clock()}, 1)
        after = self.fx.service.life_snapshot()
        self.assertAlmostEqual(before - after["energy"], .088, places=4)
        self.assertEqual(after["phase"], "active")

    def test_recovery_and_energy_config_are_bounded_and_allow_zero_cost(self):
        import os
        for key, value in (("BINES_LIFE_REST_RECOVERY_MINUTES", "nan"),
                           ("BINES_LIFE_REST_RECOVERY_MINUTES", "0"),
                           ("BINES_LIFE_IDLE_ENERGY_PER_HOUR", "-0.01"),
                           ("BINES_LIFE_WORK_ENERGY_PER_HOUR", "inf")):
            with patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                LifeConfig.from_env()
        with patch.dict(os.environ, {"BINES_LIFE_IDLE_ENERGY_PER_HOUR": "0",
                                     "BINES_LIFE_WORK_ENERGY_PER_HOUR": "0"}):
            config = LifeConfig.from_env()
        self.assertEqual(_energy_delta("active", 3600, config)[0], 0)
        self.assertEqual(config.work_energy_per_hour, 0)

    def test_internal_or_cancelled_tools_are_not_counted_as_external_activity(self):
        ledger = self.fx.service.life
        for index, action in enumerate(("get_time", "search_memory", "regulate_life", "activity_continue")):
            ledger.record_experience("owner", f"internal-tool:{index}", {
                "category": "tool", "action": action, "autonomous": True,
                "status": "succeeded", "finished_at": self.fx.clock()}, 1)
        ledger.record_experience("owner", "cancelled-browser", {
            "category": "tool", "action": "website_open", "autonomous": True,
            "status": "skipped", "finished_at": self.fx.clock()}, 1)
        self.assertEqual(ledger.snapshot()["today"]["autonomous_actions"], 0)
        ledger.record_experience("owner", "actual-browser", {
            "category": "tool", "action": "website_open", "autonomous": True,
            "status": "succeeded", "finished_at": self.fx.clock()}, 1)
        self.assertEqual(ledger.snapshot()["today"]["autonomous_actions"], 1)

    def test_activity_goal_spans_batches_pause_restart_and_model_finish(self):
        def batch(index, mode, topic="观察海鸟"):
            record = {
                "category": "turn", "action": "autonomous_turn", "autonomous": True,
                "turn_id": f"bird-batch:{index}", "candidate": {
                    "id": f"candidate:{index}", "kind": "curiosity", "topic": topic,
                    "activity": {"id": "bird-activity"} if index > 1 else {},
                },
                "activity": {"id": "bird-activity", "mode": mode, "topic": topic,
                             "intent": "看看接下来的捕食动作"},
                "status": "completed", "finished_at": self.fx.clock(), "result": "真实观察",
            }
            self.fx.service.record_experience(local_env(self.fx.clock()), f"bird:{index}", record, 1)
        batch(1, "continue")
        self.fx.clock.advance(1)
        batch(2, "pause")
        goals = self.fx.service.life_snapshot()["goals"]
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["status"], "paused")
        self.assertEqual(goals[0]["batches"], 2)
        self.fx.clock.advance(7 * 3600)
        restarted = LifeLedger(self.fx.repo, self.fx.clock)
        self.assertEqual(restarted.snapshot()["goals"][0]["status"], "paused")
        batch(3, "review", "海鸟如何协作")
        self.assertEqual(self.fx.service.life_snapshot()["goals"][0]["status"], "active")
        batch(4, "finish", "海鸟如何协作")
        goals = self.fx.service.life_snapshot()["goals"]
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["status"], "completed")
        self.assertEqual(goals[0]["title"], "海鸟如何协作")
        self.assertEqual(goals[0]["batches"], 4)

    def test_completed_runtime_turn_preserves_activity_in_memory_journal(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        outbox = MemoryOutbox(self.fx.tmp / "runtime-outbox.db")
        candidate = {
            "id": "next-batch", "kind": "curiosity", "topic": "观察海鸟", "intent": "观察捕食",
            "activity": {"id": "persistent-bird", "sequence": 5},
        }
        journal = MemoryJournal(outbox, local_env(self.fx.clock()), "next-batch",
                                autonomous=True, candidate=candidate)
        with patch("common.memory_journal.time.time", side_effect=self.fx.clock):
            with tool_context(ToolContext(LOCAL_IDENTITY, journal=journal)) as context:
                context.activity.update(mode="continue", next_intent="看下一段协作", topic="海鸟协作")
                self.fx.clock.advance(5)
        outbox.sync(lambda method, **params: getattr(self.fx.service, method)(**params))
        goal = self.fx.service.life_snapshot()["goals"][0]
        self.assertEqual(goal["activity_id"], "persistent-bird")
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["title"], "海鸟协作")

    def test_terminal_audit_preserves_measured_work_time(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        outbox = MemoryOutbox(self.fx.tmp / "audit-outbox.db")
        journal = MemoryJournal(outbox, local_env(self.fx.clock()), "audit-turn",
                                autonomous=True, candidate={"id": "audit-turn", "topic": "海鸟"})
        with patch("common.memory_journal.time.time", side_effect=self.fx.clock):
            with tool_context(ToolContext(LOCAL_IDENTITY, journal=journal)):
                self.fx.clock.advance(45)
            self.fx.clock.advance(100)
            journal.capture("autonomous_turn", {}, "终态回执", category="turn",
                            status="completed", key=journal.prefix + ":turn")
        with outbox.connection() as db:
            record = json.loads(db.execute("SELECT params FROM memory_outbox").fetchone()[0])["record"]
        self.assertEqual(record["duration_seconds"], 45)
        self.assertEqual(record["result"], "终态回执")

    def test_eight_hours_many_internal_records_preserve_one_active_goal(self):
        from common.activity import activity_checkpoint
        start = self.fx.clock()
        policy = ProactivePolicy(start)
        user = {"id": "1", "ts": start-2000, "content": "随意探索"}
        policy.tick(start, user)
        candidate = {"id": "long-life", "topic": "观察海鸟", "kind": "curiosity",
                     "intent": "观察不同海鸟的行为", "reason": "对不同行为有兴趣", "based_on_user": "1",
                     "timestamp": start, "contact_allowed": True}
        checkpoint = {}
        for batch in range(96):  # Eight hours, default lifecycle and policy parameters.
            self.fx.clock.advance(300)
            now = self.fx.clock()
            life = self.fx.service.life_snapshot()
            self.assertEqual(life["phase"], "active")
            self.assertTrue(policy.tick(now, user, life=life))
            candidate = {**candidate, "id": f"batch:{batch}", "timestamp": now,
                         "activity": checkpoint}
            policy.reserve(candidate)
            checkpoint = activity_checkpoint(candidate, {"mode": "continue", "delay_seconds": 2,
                                                         "decision_source": "model"}, [])
            policy.resolve(now, delivered=True, activity=checkpoint)
            ledger = self.fx.service.life
            for index in range(24):
                ledger.record_experience("owner", f"audit:{batch}:{index}", {
                    "category": "feedback", "status": "completed", "finished_at": now}, 1)
            ledger.record_experience("owner", f"batch:{batch}", {
                "category": "turn", "action": "autonomous_turn", "autonomous": True,
                "turn_id": f"batch:{batch}", "candidate": candidate, "activity": checkpoint,
                "status": "completed", "duration_seconds": 90, "finished_at": now}, 1)
        result = self.fx.service.life_snapshot()
        self.assertGreater(result["energy"], .18)
        self.assertLess(result["sleep_pressure"], .88)
        self.assertEqual(result["goals"][0]["status"], "active")
        self.assertEqual(result["goals"][0]["batches"], 96)
        self.assertEqual(policy.state.actions_today, 1)

    def test_regulation_rejects_unsafe_scope_duration_and_churn(self):
        with self.assertRaises(PermissionError):
            self.fx.service.regulate_life(
                "qq:test:private:stranger", "rest", 30, "无权改变全局生命", "stranger"
            )
        for mode, minutes in (("rest", 4), ("rest", 481), ("wake", 241), ("unknown", 30)):
            with self.subTest(mode=mode, minutes=minutes), self.assertRaises(ValueError):
                self.fx.service.regulate_life(
                    "owner", mode, minutes, "超出安全边界", f"invalid:{mode}:{minutes}"
                )
        self.fx.service.regulate_life(
            "owner", "wake", 30, "短暂保持清醒", "wake-cooldown"
        )
        with self.assertRaisesRegex(ValueError, "刚调整过"):
            self.fx.service.regulate_life(
                "owner", "rest", 30, "不应立刻反复切换", "rest-too-soon"
            )
        self.fx.clock.advance(1)
        self.fx.service.record_message(
            local_env(self.fx.clock()), "user", "现在可以改为休息", source_ref="regulation-user"
        )
        changed = self.fx.service.regulate_life(
            "owner", "rest", 30, "根据新的真人消息改为休息", "rest-after-user"
        )
        self.assertEqual(changed["life"]["phase"], "resting")
        automatic = self.fx.service.regulate_life(
            "owner", "auto", 0, "恢复自动生命节律", "return-auto"
        )
        self.assertEqual(automatic["life"]["regulation"], {})


class LifePolicyTests(unittest.TestCase):
    def test_sleep_allows_internal_planning_and_active_phase_restores_authority(self):
        now = stamp()
        config = ProactiveConfig(interval=1, min_idle=0, startup_grace=0, quiet_start=0, quiet_end=0)
        user = {"id": 1, "ts": now - 1000, "content": "晚点聊"}
        sleeping = ProactivePolicy(now, config)
        sleeping.tick(now, user)
        for second in range(1, 20):
            ready = sleeping.tick(now + second, user, life={"phase": "sleeping", "energy": 0.9})
        self.assertTrue(ready)
        self.assertTrue(sleeping.state.planning_only)
        self.assertLess(sleeping.state.value, 45)
        for second in range(20, 40):
            ready = sleeping.tick(now + second, user, life={"phase": "resting", "energy": 0.6})
        self.assertTrue(ready)
        self.assertFalse(sleeping.contact_permissions(now+39, user)["contact_allowed"])
        active = ProactivePolicy(now, config)
        active.tick(now, user)
        for second in range(1, 20):
            ready = active.tick(now + second, user, life={"phase": "active", "energy": 0.9})
        self.assertTrue(ready)
        compact = compact_life({"phase": "active", "energy": 0.9, "goals": [
            {"title": f"目标{i}", "intent": "继续", "kind": "initiative", "status": "active"}
            for i in range(8)
        ]})
        self.assertEqual(len(compact["active_goals"]), 3)


class LifeToolTests(unittest.TestCase):
    def test_local_tool_uses_trusted_turn_source_and_returns_bounded_state(self):
        from tools.dependencies import deps
        from tools.life_tool import regulate_life
        client = Mock()
        client.regulate_life.return_value = {
            "status": "updated",
            "life": {
                "phase": "resting", "phase_label": "自主休息", "energy": 0.2,
                "sleep_pressure": 0.9,
                "regulation": {"mode": "rest", "reason": "需要恢复", "until": stamp() + 1800},
            },
        }
        with patch.object(deps, "memory_client", client), \
                tool_context(ToolContext(LOCAL_IDENTITY, "event", "turn-1")):
            result = regulate_life("rest", "需要恢复", 30)
        client.regulate_life.assert_called_once_with(
            "owner", "rest", 30, "需要恢复", "local-life-regulation:turn-1"
        )
        self.assertEqual(result["phase"], "resting")
        self.assertNotIn("born_at", result)

    def test_untrusted_identity_cannot_call_life_tool(self):
        from tools.life_tool import regulate_life
        stranger = Identity("99", "2")
        with tool_context(ToolContext(stranger, "event", "turn")), self.assertRaises(PermissionError):
            regulate_life("wake", "无权保持清醒", 30)


if __name__ == "__main__":
    unittest.main()
