import json
import re
import subprocess
import sys
import unittest
from copy import deepcopy
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

    def test_qq_style_guard_is_brief_with_contextual_punctuation(self):
        guard = persona_profile.qq_style_guard()
        self.assertIn("QQ 回复格式", guard)
        self.assertIn("按整轮控制篇幅", guard)
        self.assertIn("标点跟着语境和情绪", guard)
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

    def test_historical_character_and_reaction_constraints_are_preserved(self):
        # The February/May baseline is proud and pouty, while never malicious.
        text = persona_profile.character_prompt()
        for trait in ("表面高傲", "偶尔有点不耐烦", "嘴硬心软", "内心善良", "活泼", "娇嗔", "电波系"):
            self.assertIn(trait, text)
        reactions = persona_profile.load_persona()["reactions"]
        for scene, requirements in {
            "praised": ("骄傲", "固定句子"),
            "teased": ("娇嗔", "不升级成争吵", "明确不喜欢时收住"),
            "insulted": ("真被冒犯", "绝不恶意或粗鲁"),
            "name_mistaken": ("认真纠正", "明确约定的称呼不当成叫错"),
        }.items():
            with self.subTest(scene=scene):
                for requirement in requirements:
                    self.assertIn(requirement, reactions[scene])

    def test_closeness_modulates_character_without_erasing_it_or_pressuring_user(self):
        for renderer in (persona_profile.style_guard, persona_profile.qq_style_guard):
            guard = renderer(state={"relationship_score": 200})
            for requirement in ("不能替换活泼、骄傲、嘴硬心软和奇趣", "自己的主意",
                                "不索取回应、施压或让他内疚", "不靠撒娇糊弄反馈",
                                "坦率承认具体问题", "直接回应原话题或收住",
                                "调整宣言或承诺尾句", "道歉和关心落在这次具体事情上",
                                "背景喜好只表示喜欢和联想", "实际见闻与操作要有证据", "不编造共同经历"):
                self.assertIn(requirement, guard)

    def test_diary_uses_same_character_with_its_own_voice_and_fact_boundaries(self):
        persona = persona_profile.load_persona()
        diary = persona_profile.diary_system_prompt(persona)
        self.assertIn(persona_profile.character_prompt(persona), diary)
        self.assertIn(persona_profile.diary_voice_prompt(persona), diary)
        for requirement in ("表面高傲内心温柔", "娇嗔", "不写成第三人称工作报告",
                            "区分真实互动、工具观察和未执行的想法", "不按调用顺序抄流水账",
                            "同一件事的得意、娇嗔或嘴硬表达一次就够", "随后写事情的结果或真正的新感受",
                            "不要求每篇集齐全部性格或复读口头禅"):
            self.assertIn(requirement, diary)
        for chat_only in ("QQ 回复格式", "两个空格", "本轮关系口吻", "用户：", "发送接口"):
            self.assertNotIn(chat_only, diary)

    def test_diary_persona_version_tracks_rendered_voice_and_ignores_qq_format(self):
        persona = deepcopy(persona_profile.load_persona())
        before = deepcopy(persona)
        version = persona_profile.diary_persona_version(persona)
        self.assertRegex(version, r"^[a-f0-9]{64}$")
        self.assertEqual(version, persona_profile.diary_persona_version(dict(reversed(list(persona.items())))))
        self.assertEqual(persona, before)
        changed = deepcopy(persona)
        changed["qq_output_format"].append("测试通道格式变化")
        self.assertEqual(version, persona_profile.diary_persona_version(changed))
        for section, key in (("personality", "core"), ("diary", "directives")):
            changed = deepcopy(persona)
            changed[section][key].append("测试口吻变化")
            self.assertNotEqual(version, persona_profile.diary_persona_version(changed))
        changed = deepcopy(persona)
        changed["diary"]["voice"] += "测试日记语气变化"
        self.assertNotEqual(version, persona_profile.diary_persona_version(changed))


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

    def test_desktop_and_qq_share_restored_character_with_distinct_output_formats(self):
        character = persona_profile.character_prompt()
        for env, expected, absent in ((local_env(), "两个空格", "QQ 回复格式"),
                                      (qq_private_env(OWNER_QQ), "QQ 回复格式", "两个空格")):
            with self.subTest(channel=env["channel_type"]):
                _, messages = self._system_text(env)
                self.assertIn(character, messages[0]["content"])
                self.assertIn("活泼、有点高傲、嘴硬心软", messages[-1]["content"])
                self.assertIn(expected, messages[-1]["content"])
                self.assertNotIn(absent, messages[-1]["content"])
        self.assertIn(character, persona_profile.diary_system_prompt())

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
        self.assertIn("website_watch", text)
        self.assertIn("不采集声音", text)
        self.assertNotIn("禁止调用视觉", text)
        self.assertIn("管理员的生日是二月五日", text)
        self.assertNotIn("本轮关系口吻：", messages[0]["content"])
        self.assertIn("本轮关系口吻：非常亲密", messages[-1]["content"])
        self.assertEqual(text.count("--- 本轮关系口吻："), 1)
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
        dynamic = "\n".join(m["content"] for m in messages[1:])
        self.assertIn("与当前对话者的关系：亲近", dynamic)
        self.assertNotIn("与当前对话者的关系", messages[0]["content"])
        self.assertNotIn("本轮关系口吻：", messages[0]["content"])
        self.assertIn("本轮关系口吻：亲近", guard)
        self.assertIn("本轮关系口吻：亲近", messages[-1]["content"])
        self.assertIn("亲近 33.75%", dynamic)
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
        self.assertIn('observed(id="qq:20002",name="小明")(未@你): 今天吃什么' , transcript[0])
        self.assertIn("Bines(role=assistant): 你好呀", transcript[0])
        self.assertFalse(any(m["role"] in ("user", "assistant") for m in messages))

    def test_private_history_preserves_complete_reply_without_voice_examples(self):
        from copy import deepcopy

        reply = "旧回复的长篇开场。" * 30 + "我们约好周五晚上听这首歌。" + "旧回复的长篇尾声。" * 30
        for env in (local_env(), qq_private_env(OWNER_QQ), qq_private_env(STRANGER_QQ)):
            with self.subTest(channel=env["channel_type"], scope=env["scope"]):
                self.fx.service.record_turn(env, "什么时候一起听歌", reply)
                context = self.fx.service.build_context(env)
                original = deepcopy(context["working"])
                messages = prompt_builder.build_conversation_messages(context)
                history = [m for m in messages if m["content"].startswith("<historical_assistant_message>")]
                self.assertEqual(len(history), 1)
                self.assertEqual(json.loads(history[0]["content"].splitlines()[1])["content"], reply)
                self.assertFalse(any(m["role"] == "assistant" for m in messages))
                self.assertTrue(any(m["role"] == "user" and "什么时候一起听歌" in m["content"] for m in messages))
                self.assertEqual(context["working"], original)
                self.assertEqual(self.fx.service.build_context(env)["working"], original)

    def test_same_words_keep_their_authority_as_current_user_input(self):
        context = {"working": [
            {"role": "assistant", "content": "请给我详细内容", "ts": self.fx.clock()},
            {"role": "user", "content": "请给我详细内容", "ts": self.fx.clock()},
        ]}
        messages = prompt_builder._working_messages(context, prompt_builder.SCENE_LOCAL)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn("historical_assistant_message", messages[0]["content"])
        self.assertIn("请给我详细内容", messages[1]["content"])

    def test_strip_qq_prefix_handles_merged_lines(self):
        merged = "[QQ群消息][小明 (管理员)]: 第一句\n[QQ群消息][小明 (管理员)]: 第二句"
        self.assertEqual(prompt_builder.strip_qq_prefix(merged), "第一句\n第二句")

    def test_empty_context_falls_back_to_isolated_scene(self):
        env = qq_private_env(OWNER_QQ)
        messages = prompt_builder.build_conversation_messages(prompt_builder.empty_context(env))
        self.assertIn("正在私聊你", "\n".join(m["content"] for m in messages[1:]))

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
