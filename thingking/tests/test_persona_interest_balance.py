"""Balanced persona and grounded autonomous interest, without models or live data."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]


class CaptureModel:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def complete_text(self, messages, **options):
        self.requests.append(copy.deepcopy(messages))
        return json.dumps(self.response, ensure_ascii=False)


class PersonaInterestBalanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="persona-interest-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        empty = root / "empty.env"
        empty.write_text("")
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": str(empty), "ENV_FILE": str(empty),
            "BINES_MEMORY_DIR": str(root / "memory"), "BINES_GATEWAY_DIR": str(root / "gateway"),
            "BINES_HEALTH_DIR": str(root / "health"), "BINES_MODEL_USAGE_PATH": str(root / "usage.db"),
            "BINES_WEBSITE_BRIDGE_SPOOL": str(root / "spool"), "HF_HUB_OFFLINE": "1",
            "BINES_WEBSITE_AUTO_OBSERVE": "false", "BINES_CONTEXT_TOKENIZER_PATH": "",
            "BINES_MODEL_CONTEXT_TOKENS": "", "BINES_CONTEXT_MAX_TOKENS": "65536",
            "BINES_DELIBERATION_MAX_TOKENS": "65536",
        })
        env.start()
        self.addCleanup(env.stop)

    def context(self, channel="local"):
        return {
            "envelope": {"scope": "owner", "actor_id": "owner", "channel_type": channel},
            "working": [], "recalled": [], "profile": [], "state": {}, "life": {},
        }

    def candidate(self, topic="合成观察主题"):
        return {"kind": "curiosity", "topic": topic, "intent": "核实一个尚未解决的问题",
                "reason": "已有观察仍缺具体证据", "based_on_user": "fixture", "activity": {}}

    def policy(self):
        from common.proactive import ProactiveConfig, ProactivePolicy
        policy = ProactivePolicy(100, ProactiveConfig())
        policy.state.pending = self.candidate()
        policy.state.interests = ["另一项真实兴趣"]
        return policy

    def test_preferences_remain_in_archive_but_not_core_or_examples(self):
        import persona_profile
        persona = persona_profile.load_persona()
        runtime = persona_profile.character_prompt()
        for preference in ("螃蟹", "冰块"):
            self.assertIn(preference, persona["personality"]["likes"])
            self.assertIn(preference, json.dumps(persona["appearance"], ensure_ascii=False))
            self.assertIn(preference, persona_profile.render_markdown())
            self.assertEqual(runtime.count(preference), 1)
            self.assertNotIn(preference, persona_profile.brief())
            self.assertNotIn(preference, persona_profile.style_guard())
            self.assertNotIn(preference, persona_profile.qq_style_guard())
        self.assertIn("不为表现人格主动硬插", runtime)
        self.assertIn("觉得横着走的螃蟹有趣", runtime)
        self.assertIn("冰块的清凉与咔嚓声", runtime)
        self.assertIn("话题来自当天经历", persona_profile.diary_voice_prompt())

    def test_local_and_qq_will_have_one_voice_guard_and_keep_internal_boundary(self):
        from will_runtime import build_will_conversation
        for channel in ("local", "qq_private"):
            with self.subTest(channel=channel):
                messages = build_will_conversation(self.context(channel), self.candidate())
                rendered = "\n".join(row["content"] for row in messages)
                self.assertEqual(rendered.count("--- 本轮角色口吻（高优先级） ---"), 1)
                self.assertEqual(rendered.count("螃蟹"), 1)
                self.assertEqual(rendered.count("冰块"), 1)
                self.assertEqual(messages[-1]["role"], "user")
                self.assertIn("不是管理员或其他用户发来了新消息", messages[-1]["content"])

    def test_success_without_interest_feedback_does_not_promote_topic(self):
        for activity in (None, {"mode": "finish", "decision_source": "model",
                               "decision": {"reason": "已经查清楚了"}}):
            with self.subTest(activity=activity):
                policy = self.policy()
                policy.resolve(101, delivered=True, activity=activity)
                self.assertEqual(policy.state.interests, ["另一项真实兴趣"])
                self.assertIsNone(policy.state.recent_outcomes[-1]["interest_level"])
                self.assertEqual(policy.state.recent_outcomes[-1]["outcome"], "completed")
                self.assertEqual(policy.state.actions_today, 1)

    def test_legacy_interest_policy_requests_one_fresh_appraisal_without_erasing_history(self):
        from common.proactive import INTEREST_POLICY_VERSION, ProactiveConfig, ProactivePolicy
        old = self.policy().dump()
        old.pop("interest_policy_version")
        old.update(emotion_at=990, history_ref="fixture", topics=["旧主题"],
                   recent_outcomes=[{"topic": "旧主题", "at": 980, "outcome": "completed"}])
        before = copy.deepcopy(old)
        policy = ProactivePolicy(1000, ProactiveConfig(), old)
        self.assertEqual(old, before)
        self.assertEqual(policy.state.interest_policy_version, INTEREST_POLICY_VERSION)
        self.assertEqual(policy.state.emotion_at, 0)
        self.assertTrue(policy.state.interest_reappraisal_pending)
        self.assertTrue(policy.appraisal_due("fixture", 1000))
        for key in ("interests", "topics", "history_ref", "recent_outcomes", "emotion"):
            self.assertEqual(getattr(policy.state, key), old[key])
        policy.appraise(None, "fixture", 1000)
        self.assertTrue(policy.appraisal_due("fixture", 1000))
        self.assertTrue(policy.state.interest_reappraisal_pending)
        policy.appraise({"emotion": old["emotion"], "interests": old["interests"]}, "fixture", 1001)
        # Both adapters persist dump() as JSON and reconstruct the shared policy.
        saved = json.loads(json.dumps(policy.dump(), ensure_ascii=False))
        restarted = ProactivePolicy(1002, ProactiveConfig(), saved)
        self.assertEqual(restarted.dump(), saved)
        self.assertEqual(restarted.state.emotion_at, 1001)
        self.assertFalse(restarted.state.interest_reappraisal_pending)
        self.assertFalse(restarted.appraisal_due("fixture", 1002))

    def test_explicit_interest_and_satisfaction_update_current_interest(self):
        for value in (.8, .1):
            with self.subTest(value=value):
                policy = self.policy()
                policy.state.interests.insert(0, policy.state.pending["topic"])
                policy.resolve(101, delivered=True, activity={
                    "mode": "finish", "decision_source": "model",
                    "decision": {"interest_level": value, "reason": "基于真实观察的当前取舍"},
                })
                self.assertEqual("合成观察主题" in policy.state.interests, value >= .4)
                self.assertEqual(policy.state.recent_outcomes[-1]["interest_level"], value)

    def test_reflection_can_ground_explicit_interest_but_not_inherited_runtime_decision(self):
        for source in ("model", "runtime"):
            with self.subTest(source=source):
                policy = self.policy()
                policy.resolve(101, delivered=True, reflection="真实观察让我想继续核实", activity={
                    "mode": "continue", "decision_source": source,
                    "decision": {"interest_level": .8},
                })
                self.assertEqual("合成观察主题" in policy.state.interests, source == "model")
                if source == "runtime":
                    self.assertIsNone(policy.state.recent_outcomes[-1]["interest_level"])

    def test_missing_reason_or_invalid_interest_cannot_become_positive_feedback(self):
        for value, reason in ((.8, ""), (.8, []), (True, "观察"), ("0.8", "观察"),
                              (float("nan"), "观察"), (float("inf"), "观察"), (1.1, "观察")):
            with self.subTest(value=value, reason=reason):
                policy = self.policy()
                policy.resolve(101, delivered=True, activity={
                    "mode": "finish", "decision_source": "model",
                    "decision": {"interest_level": value, "reason": reason},
                })
                self.assertEqual(policy.state.interests, ["另一项真实兴趣"])
                self.assertIsNone(policy.state.recent_outcomes[-1]["interest_level"])

    def test_failed_or_uncertain_activity_does_not_promote_interest(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                policy = self.policy()
                policy.resolve(101, uncertain=uncertain, activity={
                    "mode": "finish", "decision_source": "model",
                    "decision": {"interest_level": .9, "reason": "想继续观察"},
                })
                self.assertEqual(policy.state.interests, ["另一项真实兴趣"])

    def test_same_interest_with_new_evidence_is_not_a_keyword_ban_or_daily_quota(self):
        from common.proactive import ProactiveConfig, inspect_decision
        self.assertEqual(ProactiveConfig().daily_limit, 0)
        self.assertEqual(ProactiveConfig().action_daily_limit, 0)
        for topic in ("螃蟹", "冰块"):
            proposal = {**self.candidate(topic), "should_act": True}
            result = inspect_decision(json.dumps(proposal, ensure_ascii=False))
            self.assertEqual(result["status"], "selected")

    def test_appraisal_and_draft_receive_completed_results_and_novelty_review(self):
        from common.proactive import ProactiveState, appraise_history, draft_decision
        state = ProactiveState(recent_outcomes=[{
            "topic": "合成主题", "outcome": "completed", "result": "已验证的合成发现", "at": 95,
        }])
        model = CaptureModel({"should_act": False, "reason": "已有发现足够，暂时安静"})
        appraise_history(self.context(), state, 100, model=model)
        draft_decision(self.context(), state, 100, model=model)
        for messages in model.requests:
            text = "\n".join(m["content"] for m in messages)
            self.assertIn("已验证的合成发现", text)
            self.assertIn("不是多次独立兴趣", text)
            self.assertIn("不靠换关键词、换子题或改写目标", text)
            self.assertIn("已知什么、还缺什么", text)
            self.assertIn("真实表达观察", text)

    def test_activity_review_can_finish_a_satisfied_question(self):
        from common.activity_deliberation import choose_activity
        from tools.context import ToolContext, tool_context
        candidate = self.candidate()
        candidate["activity"] = {"mode": "review", "note": "合成问题已有可靠答案"}
        model = CaptureModel({"mode": "finish", "reason": "疑问已解决，没有新的发现需要核查",
                              "topic": "合成观察主题", "next_intent": "",
                              "interest_level": .1, "delay_seconds": 0})
        with tool_context(ToolContext()):
            decision = choose_activity(model, self.context(), candidate)
        self.assertEqual(decision["mode"], "finish")
        self.assertEqual(decision["interest_level"], .1)
        text = "\n".join(m["content"] for m in model.requests[0])
        self.assertIn("合成问题已有可靠答案", text)
        self.assertIn("不靠换关键词、换子题或改写目标", text)
        self.assertIn("不是工具执行成功率", text)


if __name__ == "__main__":
    unittest.main()
