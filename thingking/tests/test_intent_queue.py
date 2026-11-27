"""Multiple durable intentions with serial execution and independent waits."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common.activity import complete_activity, save_desktop_handoff, consume_desktop_handoff, activity_store, desktop_handoff
from common.proactive import ProactiveConfig, ProactivePolicy
from tools.context import ToolContext


class IntentQueueTest(unittest.TestCase):
    def setUp(self):
        self.now = 1794374400.0
        self.user = {"id": "owner:1", "content": "我去上课", "ts": self.now - 2000}
        self.config = ProactiveConfig(startup_grace=0, min_idle=0, quiet_start=0, quiet_end=0)
        self.policy = ProactivePolicy(self.now, self.config, {"user_ref": "owner:1"})
        control = patch("common.activity.activity_control", return_value={})
        control.start(); self.addCleanup(control.stop)

    def checkpoint(self, name="lunch", **changes):
        return {"id": name, "kind": "connection", "mode": "pause", "topic": "等下课",
                "intent": "下课后再看是否想聊", "reason": "现在在上课", "note": "原始进度",
                "sequence": 2, "delay_seconds": 1800, "next_at": self.now + 1800,
                "updated_at": self.now, "decision_source": "model", **changes}

    def test_legacy_single_pause_releases_focus_without_shortening_its_deadline(self):
        original = self.checkpoint()
        self.policy = ProactivePolicy(self.now, self.config, {
            "user_ref": "owner:1", "activity": original, "next_decision": self.now + 1800})
        self.assertFalse(self.policy.tick(self.now, self.user))
        self.assertEqual(self.policy.state.activity, {})
        self.assertEqual(self.policy.state.waiting_activities["lunch"], original)
        self.assertTrue(self.policy.tick(self.now + 61, self.user))
        self.assertEqual(self.policy.state.waiting_activities["lunch"]["next_at"], self.now + 1800)

    def test_two_independent_waits_survive_restart_and_finish_only_one(self):
        self.policy.accept_activity(self.checkpoint(), self.now)
        self.policy.tick(self.now + 61, self.user)
        self.policy.accept_activity(self.checkpoint("learning", kind="curiosity", topic="说话风格",
                                                  next_at=self.now+600, delay_seconds=600), self.now+62)
        self.policy.tick(self.now+123, self.user)
        restored = ProactivePolicy(self.now+124, self.config, self.policy.dump())
        self.assertEqual(set(restored.state.waiting_activities), {"lunch", "learning"})
        restored.tick(self.now+601, self.user)
        self.assertEqual(restored.state.activity["id"], "learning")
        self.assertEqual(restored.state.activity["mode"], "review")
        restored.accept_activity({"id": "learning", "mode": "finish", "sequence": 3}, self.now+602)
        self.assertEqual(set(restored.state.waiting_activities), {"lunch"})
        restored.tick(self.now+1801, self.user)
        self.assertEqual(restored.state.activity["id"], "lunch")

    def test_new_activity_result_does_not_erase_waiting_intent(self):
        self.policy.accept_activity(self.checkpoint(), self.now)
        self.policy.tick(self.now+61, self.user)
        self.policy.reserve({"id": "search", "kind": "curiosity", "topic": "新的兴趣",
            "intent": "看看新资料", "reason": "想了解", "based_on_user": "owner:1", "timestamp": self.now+61})
        self.policy.resolve(self.now+62, delivered=True)
        self.assertIn("lunch", self.policy.state.waiting_activities)

    def test_cas_rejects_stale_handoff_and_stale_finish(self):
        original = self.checkpoint(sequence=10)
        self.policy.accept_activity(original, self.now)
        for cp in (self.checkpoint(sequence=2, intent="旧进度"), self.checkpoint(sequence=2, mode="finish")):
            self.policy.accept_activity(cp, self.now+1)
            self.assertEqual(self.policy.state.activity, original)

    def test_pending_lease_prevents_switching_focus(self):
        self.policy.state.activity = self.checkpoint("working", mode="continue", next_at=self.now)
        self.policy.state.waiting_activities["waiting"] = self.checkpoint("waiting", next_at=self.now)
        self.policy.state.pending = {"id": "owned-by-worker"}
        before = copy.deepcopy(self.policy.state.activity)
        self.assertFalse(self.policy.tick(self.now+61, self.user))
        self.assertEqual(self.policy.state.activity, before)
        self.assertEqual(set(self.policy.state.waiting_activities), {"waiting"})

    def test_oldest_due_activity_gets_focus_between_quanta(self):
        self.policy.state.activity = self.checkpoint("A", mode="continue", next_at=self.now+2, updated_at=self.now)
        self.policy.state.waiting_activities["B"] = self.checkpoint("B", next_at=self.now-20)
        self.policy.tick(self.now+3, self.user)
        self.assertEqual(self.policy.state.activity["id"], "B")
        self.assertIn("A", self.policy.state.waiting_activities)

    def test_active_review_backoff_is_not_reset_every_tick(self):
        self.policy.state.activity = self.checkpoint(mode="review", next_at=self.now)
        self.policy.defer(self.now, 300)
        for offset in (1, 2, 30, 120):
            self.assertFalse(self.policy.tick(self.now+offset, self.user))
        self.assertEqual(self.policy.state.next_decision, self.now+300)

    def test_short_explicit_continue_keeps_its_cadence_and_model_choice(self):
        self.policy.state.activity = self.checkpoint(mode="continue", next_at=self.now+2)
        self.policy.state.next_decision = self.now+2
        self.assertFalse(self.policy.tick(self.now+1, self.user))
        self.assertTrue(self.policy.tick(self.now+3, self.user))
        self.assertEqual(self.policy.state.activity["mode"], "continue")

    def test_ordinary_chat_does_not_rewrite_paused_activity_as_two_second_review(self):
        context = ToolContext(user_text="你看这朵云")
        cp = self.checkpoint()
        complete_activity(context, {"activity": cp, "intent": cp["intent"]}, [], "像棉花糖", ordinary=True)
        self.assertEqual(context.activity, {})
        self.assertEqual(cp, self.checkpoint())

    def test_explicit_owner_activity_control_can_update_pause(self):
        context = ToolContext(user_text="继续刚才的活动")
        context.activity.update(mode="continue", next_intent="继续刚才的目标")
        complete_activity(context, {"activity": self.checkpoint()}, [], ordinary=True)
        self.assertEqual(context.activity["mode"], "continue")

    def test_manual_verification_still_requires_owner_resume(self):
        self.policy.accept_activity(self.checkpoint(delay_seconds=0), self.now)
        self.assertTrue(self.policy.tick(self.now+10000, self.user))
        self.assertFalse(self.policy.state.activity)
        self.assertEqual(next(iter(self.policy.state.waiting_activities.values()))["mode"], "pause")
        with patch("common.activity.activity_control", return_value={"mode": "resume", "updated_at": self.now+10001}):
            self.assertTrue(self.policy.tick(self.now+10002, self.user))
        self.assertEqual(self.policy.state.activity["mode"], "review")

    def test_owner_global_stop_clears_all_intentions(self):
        self.policy.state.activity = self.checkpoint("A")
        self.policy.state.waiting_activities["B"] = self.checkpoint("B")
        with patch("common.activity.activity_control", return_value={"mode": "stop"}):
            self.assertFalse(self.policy.tick(self.now+10, self.user))
        self.assertFalse(self.policy.state.activity or self.policy.state.waiting_activities)

    def test_duplicate_waiting_goal_is_not_created_again(self):
        cp = self.checkpoint()
        self.policy.state.waiting_activities[cp["id"]] = cp
        self.assertIsNone(self.policy.record_decision({k:cp[k] for k in ("kind","topic","intent","reason")},self.now))
        self.assertEqual(self.policy.state.last_decision["status"], "waiting_duplicate")
        self.assertIsNotNone(self.policy.record_decision({"kind":"curiosity","topic":"口语表达",
            "intent":"看看自然表达的例子","reason":"新的兴趣"},self.now))

    def test_desktop_handoffs_preserve_two_goals_without_reapplying_acknowledged_handoff(self):
        with tempfile.TemporaryDirectory() as temp, patch("common.activity.control_path", return_value=Path(temp)/"control.json"):
            for name in ("A", "B"):
                save_desktop_handoff(name, self.checkpoint(name))
                self.assertTrue(consume_desktop_handoff(self.policy))
                self.assertFalse(consume_desktop_handoff(self.policy))
            self.assertEqual(self.policy.state.activity["id"], "B")
            self.assertIn("A", self.policy.state.waiting_activities)

    def test_prompt_cards_are_bounded_without_dropping_persisted_checkpoints(self):
        for i in range(12):
            self.policy.state.waiting_activities[str(i)] = self.checkpoint(str(i), observations=["private long tool output"])
        cards = self.policy.waiting_intentions()
        self.assertEqual(len(cards), 8)
        self.assertNotIn("private long tool output", json.dumps(cards))
        self.assertEqual(len(self.policy.dump()["waiting_activities"]), 12)

    def test_two_desktop_turns_before_consumption_survive_restart_and_ack_prunes_only_committed_entries(self):
        with tempfile.TemporaryDirectory() as temp, patch("common.activity.control_path", return_value=Path(temp)/"control.json"):
            save_desktop_handoff("turn:A", self.checkpoint("A"))
            save_desktop_handoff("turn:B", self.checkpoint("B"))
            self.assertTrue(consume_desktop_handoff(self.policy))
            self.assertEqual(self.policy.state.activity["id"], "B")
            self.assertIn("A", self.policy.state.waiting_activities)
            activity_store().update("desktop", lambda old: self.policy.dump())
            save_desktop_handoff("turn:C", self.checkpoint("C"))
            stored = desktop_handoff()
            self.assertEqual([h["id"] for h in stored["handoffs"]], ["turn:C"])
            restored = ProactivePolicy(self.now, self.config, self.policy.dump())
            self.assertTrue(consume_desktop_handoff(restored))
            self.assertEqual(restored.state.activity["id"], "C")
            self.assertEqual(set(restored.state.waiting_activities), {"A", "B"})


class IntentQueueGatewayTest(unittest.TestCase):
    from verification.proactive_behavior import QQTests as _Fixture
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    insert = _Fixture.insert

    def test_ordinary_chat_preserves_pause_and_scheduler_can_start_different_intention(self):
        import time
        from unittest.mock import Mock
        from chatBot.proactive import QQProactive
        from common.proactive import human_ref
        from thingking.qq_thinking import QQThinking
        from verification.napcat_integration import event
        now = time.time()
        ev = event('1', msg_id=811, text='你看这朵云', time=now)
        self.insert(ev)
        identity = self.store.identity(ev)
        with self.store.connection() as db:
            user = dict(db.execute("SELECT * FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1").fetchone())
        policy = ProactivePolicy(now, ProactiveConfig(startup_grace=0, quiet_start=0, quiet_end=0),
            {'user_ref':human_ref(user)})
        original = {'id':'lunch','kind':'connection','mode':'pause','topic':'等下课',
            'intent':'下课后再聊','reason':'现在在上课','delay_seconds':1800,'next_at':now+1800,
            'updated_at':now,'sequence':2,'decision_source':'model'}
        policy.accept_activity(original,now)
        memory=Mock();memory.activity_snapshot.return_value={'last_owner_activity':{'ts':now},'state':{}}
        memory.build_context.return_value={'working':[],'recalled':[],'state':{}}
        choose=Mock(return_value={'kind':'curiosity','topic':'自然口语','intent':'研究自然口语表达','reason':'想学新的说话方式'})
        clock=[now]
        scheduler=QQProactive(self.store,memory,self.client,config=policy.config,clock=lambda:clock[0],
            decide=choose,appraise=Mock(return_value=None))
        scheduler.policies[identity.conversation]=policy
        scheduler.save(identity.conversation,policy)
        rows=self.store.claim('synthetic',0,0)
        model=Mock();model.run_skill_protocol.return_value=('像棉花糖',[],[])
        QQThinking(self.store,self.client).process(rows,model)
        self.store.finish([r['id'] for r in rows],'synthetic')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activity_handoffs').fetchone()[0],0)
        clock[0]=now+61
        scheduler.tick()
        choose.assert_called_once()
        self.assertEqual(policy.state.waiting_activities['lunch'],original)
        self.assertEqual(policy.state.pending['kind'],'curiosity')
        self.assertEqual(policy.state.pending['activity'],{})
        self.assertEqual(choose.call_args.args[0]['waiting_intentions'][0]['id'],'lunch')
        scheduler.tick()
        choose.assert_called_once()  # Pending event cannot start a second execution.

    def test_two_unconsumed_handoffs_are_merged_by_id_once(self):
        import time
        from unittest.mock import Mock
        from chatBot.proactive import QQProactive
        from verification.napcat_integration import event
        now=time.time();conversation=self.store.identity(event('1')).conversation
        policy=ProactivePolicy(now,ProactiveConfig(startup_grace=0))
        scheduler=QQProactive(self.store,Mock(),self.client)
        for name in ('first','second'):
            cp={'id':name,'kind':'curiosity','mode':'pause','topic':name,'intent':name,
                'reason':'稍后观察','delay_seconds':600,'next_at':now+600,'sequence':1}
            self.store.begin_turn(name)
            self.store.save_turn(name,'done',{'activity':cp},handoff={'conversation':conversation,'checkpoint':cp})
        scheduler.consume_handoffs(conversation,policy,now)
        self.assertEqual(policy.state.activity['id'],'second')
        self.assertIn('first',policy.state.waiting_activities)
        before=policy.dump()
        scheduler.consume_handoffs(conversation,policy,now+1)
        self.assertEqual(policy.dump(),before)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activity_handoffs WHERE consumed=1').fetchone()[0],2)


if __name__ == "__main__":
    unittest.main()
