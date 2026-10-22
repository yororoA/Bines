import re
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import GROUP_ID, OWNER_QQ, STRANGER_QQ, THINKING_DIR, ROOT_DIR, MemoryFixture, local_env, qq_group_env, qq_private_env

import persona_profile
import prompt_builder
from memory.models import OWNER_SCOPE

PERSONA_MARKERS = ("傲娇", "仿生人保护法", "螃蟹", "158cm", "白云莺", "tsundere")
CODE_DIRS = [ROOT_DIR / "thingking" / "src", ROOT_DIR / "tools", ROOT_DIR / "server", ROOT_DIR / "chatBot"]


class PersonaSourceTest(unittest.TestCase):
    def test_persona_is_single_source(self):
        offenders = []
        for directory in CODE_DIRS:
            for path in directory.rglob("*.py"):
                if "venv" in path.parts or path.name == "persona_profile.py":
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for marker in PERSONA_MARKERS:
                    if marker in text:
                        offenders.append(f"{path.relative_to(ROOT_DIR)}: {marker}")
        self.assertEqual(offenders, [], "persona text must live only in thingking/persona/persona.yaml")

    def test_generated_doc_is_current(self):
        result = subprocess.run(
            [sys.executable, str(THINKING_DIR / "persona" / "render_doc.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_character_prompt_contains_core_sections(self):
        text = persona_profile.character_prompt()
        for expected in ("碧波", "能力边界", "傲娇", "操作类请求照常帮忙"):
            self.assertIn(expected, text)
        self.assertIn("两个空格", persona_profile.output_rules())
        guard = persona_profile.style_guard()
        self.assertIn("本轮角色口吻（高优先级）", guard)
        self.assertIn("不能退化成通用助手", guard)
        self.assertIn("安抚套话", guard)
        self.assertIn("不在回复中谈论人格提示", guard)
        self.assertIn("以下示例只约束语气", guard)

    def test_qq_style_guard_is_short_and_light_on_punctuation(self):
        guard = persona_profile.qq_style_guard()
        self.assertIn("QQ 回复格式", guard)
        self.assertIn("简洁直接", guard)
        self.assertIn("少用标点", guard)
        self.assertIn("自己决定分段", guard)
        self.assertIn("不按字数拆分", guard)
        self.assertNotIn("正常使用逗号、顿号、句号", guard)
        self.assertIn("两个空格", persona_profile.output_rules())

    def test_relationship_mood_for_every_level(self):
        from relationship_state import RELATIONSHIP_STATES

        for state in RELATIONSHIP_STATES:
            self.assertNotEqual(persona_profile.relationship_mood(state["name"]), f"当前心情：{state['name']}。")

    def test_relationship_voice_follows_score_and_keeps_stored_state(self):
        state = {"relationship_score": 133, "relationship_level": "普通"}
        guard = persona_profile.qq_style_guard(state=state)
        self.assertIn("本轮关系口吻：非常亲密", guard)
        self.assertIn("直白表达感谢与关心", guard)
        self.assertIn("真诚夸奖", guard)
        self.assertIn("日常", guard)
        self.assertIn("自己的喜好", guard)
        self.assertIn("趴着降温", guard)
        self.assertIn("嗯哼", guard)
        self.assertNotIn("别让我重复", guard)
        self.assertEqual(state["relationship_level"], "普通")
        for score, level in ((-200, "敌对"), (-80, "冷淡"), (0, "普通"), (80, "亲近"), (200, "非常亲密")):
            with self.subTest(score=score):
                self.assertEqual(persona_profile.relationship_level({"relationship_score": score}), level)

    def test_relationship_voice_does_not_invent_missing_state(self):
        self.assertNotIn("--- 本轮关系口吻：", persona_profile.qq_style_guard())
        for value in ("unknown", None, True, float("nan"), float("inf")):
            with self.subTest(score=value):
                state = {"relationship_score": value, "relationship_level": "亲近"}
                self.assertEqual(persona_profile.relationship_level(state), "亲近")


class PromptBuilderTest(unittest.TestCase):
    def setUp(self):
        self.random_sample = patch("random.random", return_value=0.99)
        self.random_sample.start()
        self.addCleanup(self.random_sample.stop)
        self.fx = MemoryFixture()
        service = self.fx.service
        service.put_state(OWNER_SCOPE, {
            "relationship_score": 133, "relationship_level": "非常亲密", "memory_highlights": ["管理员的私密约定"],
            "npc_state": {"attire": "开衫"},
        })
        service.repo.add_memory("semantic", OWNER_SCOPE, "管理员的生日是二月五日", subject_id="owner")
        service.reconcile_index()

    def tearDown(self):
        self.fx.close()

    def _system_text(self, envelope):
        messages = prompt_builder.build_conversation_messages(self.fx.service.build_context(envelope, "生日"))
        return "\n".join(m["content"] for m in messages if m["role"] == "system"), messages

    def test_local_scene_includes_owner_state_and_profile(self):
        text, _ = self._system_text(local_env())
        self.assertIn("你正在电脑前", text)
        self.assertIn("管理员的私密约定", text)
        self.assertIn("管理员的生日是二月五日", text)
        self.assertNotIn("QQ 群", text)

    def test_style_guard_is_the_last_message_before_current_input(self):
        _, messages = self._system_text(local_env())
        self.assertEqual(messages[-1]["role"], "system")
        self.assertIn("本轮角色口吻（高优先级）", messages[-1]["content"])
        self.assertIn("直接以碧波的口吻回答", messages[-1]["content"])
        self.assertIn("本轮关系口吻：非常亲密", messages[-1]["content"])

    def test_group_scene_hides_owner_private_data(self):
        text, _ = self._system_text(qq_group_env(OWNER_QQ))
        self.assertIn(f"QQ 群 {GROUP_ID}", text)
        self.assertIn("开衫", text)
        self.assertNotIn("管理员的私密约定", text)
        self.assertNotIn("管理员的生日是二月五日", text)
        self.assertNotIn("本轮关系口吻：非常亲密", text)
        self.assertIn("这条群消息来自管理员", text)
        self.assertIn("确认为管理员本人", text)
        self.assertIn("必须承认这一身份", text)
        self.assertNotIn("对方不是管理员", text)

    def test_stranger_group_scene_does_not_claim_owner_identity(self):
        text, _ = self._system_text(qq_group_env(STRANGER_QQ, name="路人"))
        self.assertIn("对方不是管理员", text)
        self.assertNotIn("确认为管理员本人", text)
        self.assertNotIn("这条群消息来自管理员", text)

    def test_stranger_private_scene_has_privacy_policy(self):
        text, _ = self._system_text(qq_private_env(STRANGER_QQ, name="路人"))
        self.assertIn("「路人」正在私聊你", text)
        self.assertIn("不要透露管理员的私人信息", text)
        self.assertNotIn("管理员的生日是二月五日", text)
        self.assertNotIn("本轮关系口吻：非常亲密", text)

    def test_owner_qq_scene_is_owner_scope(self):
        text, messages = self._system_text(qq_private_env(OWNER_QQ))
        self.assertIn("管理员正在通过 QQ 私聊找你", text)
        self.assertIn("管理员的生日是二月五日", text)
        self.assertIn("本轮关系口吻：非常亲密", messages[0]["content"])
        self.assertIn("本轮关系口吻：非常亲密", messages[-1]["content"])
        self.assertIn("QQ 回复格式", messages[-1]["content"])

    def test_stranger_private_uses_own_relationship(self):
        context = self.fx.service.build_context(qq_private_env(STRANGER_QQ))
        self.fx.service.put_state(context["scope"], {"relationship_score": -80})
        text, messages = self._system_text(qq_private_env(STRANGER_QQ))
        self.assertIn("本轮关系口吻：冷淡", messages[-1]["content"])
        self.assertNotIn("本轮关系口吻：非常亲密", text)

    def test_state_label_and_final_guard_agree_when_level_is_stale(self):
        self.fx.service.put_state(OWNER_SCOPE, {"relationship_score": 133, "relationship_level": "冷淡"})
        text, messages = self._system_text(local_env())
        self.assertIn("与当前对话者的关系：非常亲密", text)
        self.assertIn("本轮关系口吻：非常亲密", messages[-1]["content"])
        self.assertEqual(self.fx.service.get_state(OWNER_SCOPE)["data"]["relationship_level"], "冷淡")

    def test_relationship_collapses_once_and_all_prompts_share_the_sample(self):
        context = self.fx.service.build_context(local_env())
        stored_state = dict(context["state"])
        with patch("prompt_builder.collapse_relationship_level", return_value="亲近") as collapse:
            messages = prompt_builder.build_conversation_messages(context)
            guard = prompt_builder.conversation_style_guard(context)
            prompt_builder.build_conversation_messages(context)
        collapse.assert_called_once()
        weights = collapse.call_args.args[0]
        self.assertAlmostEqual(weights["亲近"], 0.3375)
        self.assertAlmostEqual(weights["非常亲密"], 0.6625)
        self.assertIn("与当前对话者的关系：亲近", messages[0]["content"])
        self.assertIn("本轮关系口吻：亲近", messages[0]["content"])
        self.assertIn("本轮关系口吻：亲近", guard)
        self.assertIn("本轮关系口吻：亲近", messages[-1]["content"])
        self.assertIn("亲近 33.75%", messages[0]["content"])
        self.assertEqual(context["state"]["relationship_turn_level"], "亲近")
        self.assertEqual(stored_state["relationship_level"], "非常亲密")
        self.assertEqual(self.fx.service.get_state(OWNER_SCOPE)["data"], stored_state)

    def test_fresh_turn_samples_again_from_same_score(self):
        first = self.fx.service.build_context(qq_private_env(OWNER_QQ))
        second = self.fx.service.build_context(qq_private_env(OWNER_QQ))
        with patch("random.random", side_effect=[0.1, 0.9]) as sample:
            first_messages = prompt_builder.build_conversation_messages(first)
            second_messages = prompt_builder.build_conversation_messages(second)
        self.assertEqual(sample.call_count, 2)
        self.assertIn("本轮关系口吻：亲近", first_messages[-1]["content"])
        self.assertIn("本轮关系口吻：非常亲密", second_messages[-1]["content"])
        self.assertEqual(first["state"]["relationship_score"], 133)
        self.assertEqual(second["state"]["relationship_score"], 133)

    def test_missing_or_invalid_score_does_not_sample(self):
        with patch("prompt_builder.collapse_relationship_level") as collapse:
            for value in (None, True, "unknown", float("nan"), float("inf")):
                context = {"state": {"relationship_score": value}}
                prompt_builder.prepare_relationship_turn(context)
                self.assertNotIn("relationship_turn_level", context["state"])
        collapse.assert_not_called()

    def test_group_working_memory_is_a_transcript(self):
        env = qq_group_env(STRANGER_QQ, name="小明", ts=self.fx.clock())
        self.fx.service.record_message(env, "observed", "今天吃什么")
        self.fx.service.record_turn(env, "@碧波 你好", "你好呀")
        _, messages = self._system_text(qq_group_env(STRANGER_QQ, name="小明"))
        transcript = [m["content"] for m in messages if m["content"].startswith("--- 群聊近况")]
        self.assertEqual(len(transcript), 1)
        self.assertIn("小明(未@你): 今天吃什么", transcript[0])
        self.assertIn("你: 你好呀", transcript[0])
        self.assertFalse(any(m["role"] in ("user", "assistant") for m in messages))

    def test_strip_qq_prefix_handles_merged_lines(self):
        merged = "[QQ群消息][小明 (管理员)]: 第一句\n[QQ群消息][小明 (管理员)]: 第二句"
        self.assertEqual(prompt_builder.strip_qq_prefix(merged), "第一句\n第二句")

    def test_empty_context_falls_back_to_isolated_scene(self):
        env = qq_private_env(OWNER_QQ)
        messages = prompt_builder.build_conversation_messages(prompt_builder.empty_context(env))
        self.assertIn("正在私聊你", messages[0]["content"])

    def test_normal_recall_uses_summary_and_original_words_are_opt_in(self):
        context = {
            "recalled": [{"kind": "episode", "day_key": "2026-01-18",
                          "content": "管理员提出帮助修复视觉模块",
                          "evidence": [{"source_type": "message", "source_ref": "old:1",
                                        "role": "assistant", "content": "长篇旧台词" * 150}]}],
            "recall_query": "视觉修复是哪天",
        }
        normal = prompt_builder._memory_recall_block(context)
        self.assertIn("管理员提出帮助修复视觉模块", normal)
        self.assertNotIn("长篇旧台词", normal)
        quoted = prompt_builder._memory_recall_block({**context, "recall_query": "当时的原话"})
        self.assertIn("长篇旧台词", quoted)
        self.assertLess(len(quoted), 800)

    def test_legacy_state_background_is_bounded_and_not_live_activity(self):
        text = prompt_builder._state_block({
            "npc_state": {"current_activity": "上次聊过的事"},
            "important_thing": "已经结束的反馈",
            "memory_highlights": ["第一段背景"] * 20 + [f"{i}:" + "历史叙述" * 80 for i in range(30)],
        })
        self.assertIn("上次记录的活动", text)
        self.assertIn("不代表本轮话题仍在继续", text)
        self.assertIn("不是当前活动、情绪或台词", text)
        self.assertEqual(text.count("第一段背景"), 1)
        self.assertLess(len(text), 2100)


class TimeGapTest(unittest.TestCase):
    def test_time_gap_uses_last_user_timestamp(self):
        import datetime
        from message_flow_utils import build_time_gap_instruction

        now = datetime.datetime(2026, 10, 5, 12, 0)
        three_days_ago = datetime.datetime(2026, 10, 2, 12, 0).timestamp()
        text = build_time_gap_instruction(three_days_ago, "你好", now)
        self.assertRegex(text, re.compile(r"3 days ago"))
        self.assertEqual(build_time_gap_instruction(None, "你好", now), "")


if __name__ == "__main__":
    unittest.main()
