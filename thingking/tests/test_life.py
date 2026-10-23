"""Persistent life continuity across time, scopes and restart."""
from datetime import datetime
import json
import unittest

from helpers import MemoryFixture, OWNER_QQ, STRANGER_QQ, local_env, qq_group_env, qq_private_env
from memory.models import ZONE
from memory.life import LifeLedger
from common.proactive import ProactiveConfig, ProactivePolicy, compact_life
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


class LifePolicyTests(unittest.TestCase):
    def test_sleep_blocks_autonomy_and_active_energy_restores_it(self):
        now = stamp()
        config = ProactiveConfig(interval=1, min_idle=0, startup_grace=0, quiet_start=0, quiet_end=0)
        user = {"id": 1, "ts": now - 1000, "content": "晚点聊"}
        sleeping = ProactivePolicy(now, config)
        sleeping.tick(now, user)
        for second in range(1, 20):
            ready = sleeping.tick(now + second, user, life={"phase": "sleeping", "energy": 0.9})
        self.assertFalse(ready)
        self.assertLess(sleeping.state.value, 45)
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


if __name__ == "__main__":
    unittest.main()
