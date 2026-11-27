import re
import json
import os
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
        self.assertEqual(offenders, [], "persona text belongs in thingking/persona/*.yaml")

    def test_generated_doc_is_current(self):
        result = subprocess.run(
            [sys.executable, str(THINKING_DIR / "persona" / "render_doc.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_character_prompt_contains_core_sections(self):
        text = persona_profile.character_prompt()
        for expected in ("我是碧波", "说话方式", "能力边界", "高性能仿生人", "操作类请求照常处理"):
            self.assertIn(expected, text)
        self.assertIn("两个空格", persona_profile.output_rules())
        guard = persona_profile.style_guard()
        self.assertIn("本轮角色口吻（高优先级）", guard)
        self.assertIn("碧波", guard)
        self.assertNotIn("你是碧波", guard)
        self.assertIn("不附动作、心理旁白", guard)
        self.assertIn("以下示例只约束语气", guard)

    def test_casual_voice_and_factual_punctuation_reach_both_channels(self):
        # The Nov 26 rewrite briefly dropped the human-cadence rules; they now
        # live in both personas' voice guards in each persona's own wording.
        for guard in (persona_profile.qq_style_guard(), persona_profile.style_guard()):
            for preference in ("嗯", "哦", "哈哈", "行吧", "句号",
                               "不模仿他的长度和句式", "话停在这儿"):
                self.assertIn(preference, guard)
        qq = persona_profile.qq_output_rules()
        self.assertIn("篇幅由内容决定", qq)
        self.assertIn("不要求固定句长", qq)
        for rule in ("正式通知", "规范标点", "保留原文标点、编号与分行", "日常闲聊不用 Markdown"):
            self.assertIn(rule, qq)
        self.assertNotIn("只保留第一行", qq)
        self.assertIn("两个空格", persona_profile.output_rules())

    def test_relationship_mood_for_every_level(self):
        from relationship_state import RELATIONSHIP_STATES

        for state in RELATIONSHIP_STATES:
            self.assertNotEqual(persona_profile.relationship_mood(state["name"]), f"当前心情：{state['name']}。")

    def test_relationship_voice_follows_score_and_keeps_stored_state(self):
        state = {"relationship_score": 133, "relationship_level": "普通"}
        guard = persona_profile.qq_style_guard(state=state)
        self.assertIn("本轮关系口吻：非常亲密", guard)
        self.assertIn("想撒娇", guard)
        self.assertIn("不影响我喜欢他", guard)
        self.assertIn("碧波：来啦 我好想你", guard)
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
        # The current Bibo is soft and associative while never malicious.
        text = persona_profile.character_prompt()
        for trait in ("喜欢好玩的东西", "我对自己的喜好很有把握", "我很珍惜舒服的感觉"):
            self.assertIn(trait, text)
        reactions = persona_profile.load_persona()["reactions"]
        for scene, requirements in {
            "praised": ("喜欢被夸", "得意"),
            "teased": ("喜欢他愿意逗我", "没接住"),
            "insulted": ("不高兴", "亲近不该是我非得忍着什么的理由"),
            "name_mistaken": ("认自己的名字", "别名"),
        }.items():
            with self.subTest(scene=scene):
                for requirement in requirements:
                    self.assertIn(requirement, reactions[scene])

    def test_closeness_modulates_character_without_erasing_it_or_pressuring_user(self):
        for renderer in (persona_profile.style_guard, persona_profile.qq_style_guard):
            guard = renderer(state={"relationship_score": 200})
            for requirement in ("具体语气由当前内容、关系和真实感受决定",
                                "不替对方断定内心", "不凭喜好、关系等级或旧状态补造当前事实",
                                "完成与否以结果为准", "不模仿他的长度和句式",
                                "话停在这儿"):
                self.assertIn(requirement, guard)

    def test_diary_uses_same_character_with_its_own_voice_and_fact_boundaries(self):
        persona = persona_profile.load_persona()
        diary = persona_profile.diary_system_prompt(persona)
        self.assertIn(persona_profile.character_prompt(persona), diary)
        self.assertIn(persona_profile.diary_voice_prompt(persona), diary)
        for requirement in ("私人日记", "不写第三人称工作报告",
                            "事实、猜测、愿望及未执行计划分别记录",
                            "工具记录只作为相关经历的材料",
                            "没有新内容时允许简短记录或留空"):
            self.assertIn(requirement, diary)
        for chat_only in ("QQ 回复格式", "两个空格", "本轮关系口吻", "发送接口"):
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

    def _system_text(self, envelope, query="生日"):
        messages = prompt_builder.build_conversation_messages(self.fx.service.build_context(envelope, query))
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
        self.assertIn("只输出发给对方的正文", messages[-1]["content"])
        self.assertIn("本轮关系口吻：非常亲密", messages[-1]["content"])

    def test_rapid_challenge_loop_gets_a_closing_note(self):
        envelope = qq_private_env(STRANGER_QQ, ts=1_000, content="我还要喊")
        rows = [
            {"id": 1, "role": "user", "content": "你先喊一声，我听听差在哪，再决定要不要指点你。", "ts": 1_000, "actor_id": f"qq:{STRANGER_QQ}", "actor_name": "白云莺"},
            {"id": 2, "role": "assistant", "content": "示范？我又不是来教你怎么喊我名字的。你先喊得像样一点再说。", "ts": 1_003},
            {"id": 3, "role": "user", "content": "名字都没喊对就让我应，便宜占得比早上还顺。", "ts": 1_006, "actor_id": f"qq:{STRANGER_QQ}", "actor_name": "白云莺"},
            {"id": 4, "role": "assistant", "content": "行，那我教你一次。你得把尾音收住，别拖得像喊人回头。", "ts": 1_009},
            {"id": 5, "role": "user", "content": "茉莉。", "ts": 1_012, "actor_id": f"qq:{STRANGER_QQ}", "actor_name": "白云莺"},
            {"id": 6, "role": "assistant", "content": "在。不过这声算预支给你的，下回还喊不喊得像样，我到时候再评。", "ts": 1_015},
            {"id": 7, "role": "user", "content": "那我还欠你一次像样的，下回你直接告诉我差哪了。", "ts": 1_018, "actor_id": f"qq:{STRANGER_QQ}", "actor_name": "白云莺"},
            {"id": 8, "role": "assistant", "content": "行，说定了。下次你喊我的名字，我就应，不再加条件。", "ts": 1_021},
        ]
        context = prompt_builder.empty_context(envelope)
        context["working"] = rows
        messages = prompt_builder.build_conversation_messages(context)
        self.assertIn("本轮对话节奏", messages[-1]["content"])
        slow = [{**row, "ts": 1_000 + index * 400} for index, row in enumerate(rows)]
        quiet = prompt_builder.build_conversation_messages({**context, "working": slow})
        self.assertNotIn("本轮对话节奏", quiet[-1]["content"])
        # A window that brings in real outside content is not treated as a loop.
        grounded = [{**row, "content": row["content"] + " 对了我刚才路上看到一只猫"} for row in rows[-4:]]
        outside = prompt_builder.build_conversation_messages(
            {**context, "working": [*rows[:4], *grounded]})
        self.assertNotIn("本轮对话节奏", outside[-1]["content"])

    def test_desktop_and_qq_share_restored_character_with_distinct_output_formats(self):
        character = persona_profile.character_prompt()
        for env, expected, absent in ((local_env(), "两个空格", "QQ 回复格式"),
                                      (qq_private_env(OWNER_QQ), "QQ 回复格式", "两个空格")):
            with self.subTest(channel=env["channel_type"]):
                _, messages = self._system_text(env)
                self.assertIn(character, messages[0]["content"])
                self.assertIn("本轮角色口吻（高优先级）", messages[-1]["content"])
                self.assertIn(expected, messages[-1]["content"])
                self.assertNotIn(absent, messages[-1]["content"])
        self.assertIn(character, persona_profile.diary_system_prompt())

    def test_clone_uses_jasmine_for_chat_proactive_diary_and_admin(self):
        from common.admin_settings import SettingsStore

        with patch.dict(os.environ, {"BINES_MODE": "clone"}):
            text, messages = self._system_text(qq_private_env(OWNER_QQ), "逗你的，你刚才当真了？")
            self.assertIn("我是茉莉", messages[0]["content"])
            self.assertIn("茉莉：", messages[-1]["content"])
            self.assertIn("茉莉：那我现在晾你一会儿", persona_profile.relationship_guard({"relationship_level": "亲近"}))
            self.assertNotIn("碧波", text)
            self.assertIn("我是茉莉", persona_profile.brief())
            self.assertIn("我是茉莉", persona_profile.diary_system_prompt())
            # Jasmine keeps the same human-cadence permission as Bibo.
            jasmine_guard = persona_profile.qq_style_guard()
            for fragment in ("嗯", "哦", "句号", "不模仿他的长度和句式", "话停在这儿"):
                self.assertIn(fragment, jasmine_guard)
            document = SettingsStore(ROOT_DIR).document("persona")
            self.assertEqual(document["content"]["identity"]["name"], "茉莉")
            self.assertTrue(document["path"].endswith("persona_clone.yaml"))
        self.assertIn("我是碧波", persona_profile.character_prompt())

    def test_group_scene_hides_owner_private_data(self):
        text, _ = self._system_text(qq_group_env(OWNER_QQ), "今天穿了什么？管理员的生日呢？")
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
        self.assertFalse(any(m["role"] == "assistant" for m in messages))
        self.assertTrue(any(m["role"] == "user" and m["content"] == transcript[0] for m in messages))

    def test_private_history_preserves_facts_without_installing_old_style_examples(self):
        from copy import deepcopy

        reply = "旧回复的长篇开场。" * 30 + "我们约好周五晚上听这首歌。" + "旧回复的长篇尾声。" * 30
        for env in (local_env(), qq_private_env(OWNER_QQ), qq_private_env(STRANGER_QQ)):
            with self.subTest(channel=env["channel_type"], scope=env["scope"]):
                self.fx.service.record_turn(env, "什么时候一起听歌", reply)
                context = self.fx.service.build_context(env)
                original = deepcopy(context["working"])
                messages = prompt_builder.build_conversation_messages(context)
                history = [m for m in messages if m["role"] == "assistant"]
                self.assertEqual(history, [])
                record = next(m["content"] for m in messages if m["content"].startswith("历史发言记录"))
                self.assertIn("我们约好周五晚上听这首歌。", record)
                self.assertEqual(record.count("旧回复的长篇开场。"), 1)
                self.assertIn("同句共30次", record)
                self.assertTrue(any(m["role"] == "user" and "什么时候一起听歌" in m["content"] for m in messages))
                self.assertEqual(context["working"], original)
                self.assertEqual(self.fx.service.build_context(env)["working"], original)

    def test_verbatim_request_reads_the_source_without_changing_speaker(self):
        old_reply = "这话我记下了。会议改到周四下午三点。"
        for env in (local_env(), qq_private_env(OWNER_QQ), qq_private_env(STRANGER_QQ)):
            with self.subTest(channel=env["channel_type"], scope=env["scope"]):
                self.fx.service.record_turn(env, "周四下午三点见", old_reply)
                self.fx.service.record_turn(env, "帮我记一下这句话", "这话我记下了")
                context = self.fx.service.build_context(env)
                original = deepcopy(context["working"])
                messages = prompt_builder.build_conversation_messages(context)
                history = [m["content"] for m in messages if m["role"] == "assistant"]
                self.assertEqual(history, [])
                notes = [m for m in messages if m["content"].startswith("历史发言记录")]
                self.assertEqual(len(notes), 2)
                self.assertEqual(notes[0]["role"], "user")
                record = json.loads(notes[0]["content"].split("\n", 1)[1])
                self.assertIn("会议改到周四下午三点。", record["content"])
                self.assertNotIn("这话我记下了", str(notes))
                self.assertEqual(record["speaker"], "Bines(role=assistant)")
                self.assertTrue(record["time"])
                exact = prompt_builder.build_conversation_messages({**context, "current_query": "你当时的原话？"})
                records = [json.loads(m["content"].split("\n", 1)[1]) for m in exact
                           if m["content"].startswith("历史发言记录")]
                self.assertEqual([r["content"] for r in records], [old_reply, "这话我记下了"])
                self.assertEqual(context["working"], original)

    def test_same_words_keep_their_authority_as_current_user_input(self):
        context = {"working": [
            {"role": "assistant", "content": "请给我详细内容", "ts": self.fx.clock()},
            {"role": "user", "content": "请给我详细内容", "ts": self.fx.clock()},
        ]}
        messages = prompt_builder._working_messages(context, prompt_builder.SCENE_LOCAL)
        self.assertEqual([m["role"] for m in messages], ["user", "user"])
        self.assertTrue(messages[0]["content"].startswith("历史发言记录"))
        self.assertIn("Bines(role=assistant)", messages[0]["content"])
        self.assertIn("user(id=", messages[1]["content"])

    def test_other_channel_group_and_recall_views_keep_human_wording_and_exact_quotes(self):
        text = "你说得对。见面改在周三14:30。"
        assistant = {"role": "assistant", "ts": self.fx.clock(), "conversation_id": "local", "content": text}
        user = {**assistant, "role": "user"}
        evidence = [{"kind": "message", "evidence": [
            {**row, "source_type": "message", "source_ref": f"message:{i}"}
            for i, row in enumerate((user, assistant))]}]
        context = {"working": [user, assistant], "other_channels": [user, assistant], "recalled": evidence}
        for query, expect in (("见面何时", 1), ("给我原话", 2)):
            scope = {**context, "current_query": query}
            group = prompt_builder._working_messages(scope, prompt_builder.SCENE_QQ_GROUP)[0]["content"]
            recall = prompt_builder._memory_recall_block(scope, record=False)
            self.assertEqual(group.count(text), expect)
            self.assertEqual(recall.count(text), 2 * expect)
            self.assertEqual(group.count("见面改在周三14:30。"), 2)
            self.assertEqual(recall.count("见面改在周三14:30。"), 4)

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

    def test_legacy_state_background_keeps_newest_and_stays_bounded(self):
        highlights = ["最旧背景"] + [f"{i}:" + "历史叙述" * 80 for i in range(30)] + ["最新背景"]
        text = prompt_builder._state_block({
            "npc_state": {"current_activity": "上次聊过的事"},
            "important_thing": "已经结束的反馈",
            "memory_highlights": highlights,
        })
        self.assertIn("上次记录的活动", text)
        self.assertIn("不代表本轮话题仍在继续", text)
        self.assertIn("不是当前活动、情绪或台词", text)
        self.assertEqual(text.count("最新背景"), 1)
        self.assertNotIn("最旧背景", text)
        self.assertLess(len(text), 2100)

    def test_scene_notes_follow_current_topic_without_overwriting_state(self):
        state = {"npc_state": {"attire": "蓝开衫", "current_activity": "上次在折纸船"},
                 "active_quest": "周六排练", "inventory": ["旧怀表"]}
        context = {"state": state, "current_query": "哪种豆花好吃", "recall_query": "旧怀表和豆花"}
        def rendered(query):
            built = prompt_builder.build_conversation_messages({**context, "current_query": query,
                "envelope": {"channel_type": "local", "scope": "owner"}})
            return "\n".join(m["content"] for m in built)
        unrelated = rendered("哪种豆花好吃")
        for detail in ("蓝开衫", "旧怀表", "上次在折纸船"):
            self.assertNotIn(detail, unrelated)
        self.assertIn("周六排练", unrelated)
        self.assertIn("蓝开衫", rendered("今天穿了什么"))
        self.assertIn("旧怀表", rendered("你的旧怀表还在吗"))
        self.assertIn("上次在折纸船", rendered("看看你现在的状态"))
        self.assertEqual(state["npc_state"]["attire"], "蓝开衫")

    def test_named_highlight_is_visible_beyond_recent_window(self):
        agreement = "答应在白露那天一起听潮汐这首歌"
        state = {"memory_highlights": [agreement, *[f"近期记录 {i}" for i in range(40)]]}
        text = prompt_builder._state_block(state, "我们之前约好什么时候听潮汐？")
        self.assertEqual(text.count(agreement), 1)
        self.assertLess(text.index(agreement), text.index("近期记录"))

    def test_legacy_highlight_values_render_without_mutating_stored_state(self):
        from common.memory_highlights import bounded_highlight_state
        for value, expected in (("完整约定", ["完整约定"]), (73, []), (0, []), ({"unknown": "旧字段"}, [])):
            with self.subTest(value=value):
                state = {"memory_highlights": value}
                self.assertEqual(bounded_highlight_state(state)["memory_highlights"], expected)
                self.assertEqual(state["memory_highlights"], value)


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
