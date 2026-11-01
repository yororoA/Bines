import unittest
from helpers import MemoryFixture, local_env, qq_group_env, qq_private_env, OWNER_QQ, STRANGER_QQ
from common.proactive import ProactiveConfig
from listener_helpers import bored_event_rejection


class WillActivityTests(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()

    def tearDown(self):
        self.fx.close()

    def test_latest_real_user_is_found_outside_assistant_tail(self):
        service = self.fx.service
        service.record_turn(local_env(self.fx.clock()), "晚安，我去睡觉了", "晚安")
        for _ in range(12):
            self.fx.clock.advance(1)
            service.record_turn(local_env(self.fx.clock()), "", "一条后台输出")
        snapshot = service.activity_snapshot(limit=2)
        self.assertEqual(snapshot["last_user"]["content"], "晚安，我去睡觉了")
        self.assertEqual(len(snapshot["messages"]), 2)

    def test_archived_recalled_and_system_events_are_not_human_activity(self):
        service = self.fx.service
        service.record_turn(local_env(self.fx.clock()), "真实发言")
        expected = service.activity_snapshot()["last_user"]["id"]
        for origin, content in (("archived", "历史"), ("recalled", "撤回"),
                                ("live", "[System Event: Will] 内部机会")):
            self.fx.clock.advance(1)
            service.repo.add_message("local", "owner", "user", content, self.fx.clock(), "2026-10-22",
                                     actor_id="owner", origin=origin)
        self.assertEqual(service.activity_snapshot()["last_user"]["id"], expected)

    def test_owner_group_activity_is_detected_without_leaking_group_content_into_tail(self):
        service = self.fx.service
        service.record_turn(local_env(self.fx.clock()), "桌面私聊")
        self.fx.clock.advance(1)
        service.record_turn(qq_group_env(OWNER_QQ, ts=self.fx.clock()), "群聊发言")
        snapshot = service.activity_snapshot()
        self.assertEqual(snapshot["last_owner_activity"]["content"], "群聊发言")
        self.assertNotIn("群聊发言", str(snapshot["messages"]))
        self.fx.clock.advance(1)
        service.record_turn(qq_private_env(STRANGER_QQ, ts=self.fx.clock()), "陌生人")
        self.assertEqual(service.activity_snapshot()["last_owner_activity"]["content"], "群聊发言")

    def test_desktop_stale_intent_and_new_human_cancel(self):
        service = self.fx.service
        service.record_turn(local_env(self.fx.clock()), "喜欢螃蟹")
        snapshot = service.activity_snapshot()
        now = self.fx.clock()+1800
        candidate = {"id": "intent", "kind": "curiosity", "topic": "螃蟹步伐",
                     "intent": "查询螃蟹步伐", "reason": "有好奇心",
                     "based_on_user": str(snapshot["last_user"]["id"]),
                     "activity_at": self.fx.clock(), "timestamp": now}
        config = ProactiveConfig(quiet_start=0, quiet_end=0)
        self.assertEqual(bored_event_rejection(candidate, snapshot, now, config=config), "")
        self.assertEqual(bored_event_rejection(candidate, snapshot, now+121, config=config), "expired")
        self.fx.clock.advance(1801)
        service.record_turn(local_env(self.fx.clock()), "现在先别操作")
        self.assertEqual(bored_event_rejection(candidate, service.activity_snapshot(), now+1, config=config),
                         "new_user_message")

    def test_desktop_followup_respects_reply_gap_without_idle_requirement(self):
        now = self.fx.clock()
        user = {"id": 1, "ts": now-30, "content": "接着聊音乐"}
        snapshot = {"last_user": user, "last_owner_activity": {"ts": now-30},
                    "messages": [user, {"role": "assistant", "ts": now-20}]}
        candidate = {"id": "followup", "kind": "connection", "topic": "音乐",
                     "intent": "接着聊刚才的节奏", "reason": "对刚才的音乐好奇",
                     "based_on_user": "1", "activity_at": now-30, "timestamp": now,
                     "contact_mode": "followup", "reply_at": now-20}
        config = ProactiveConfig(quiet_start=0, quiet_end=0)
        self.assertEqual(bored_event_rejection(candidate, snapshot, now, config=config), "")
        snapshot["messages"][-1]["ts"] = now-19
        self.assertEqual(bored_event_rejection(candidate, snapshot, now, config=config), "not_idle")
        candidate.update(kind="curiosity")
        self.assertEqual(bored_event_rejection(candidate, snapshot, now, config=config), "")
