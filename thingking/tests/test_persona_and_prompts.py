import re
import subprocess
import sys
import unittest

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
        self.assertIn("避免通用助手式寒暄", guard)
        self.assertIn("不在回复中谈论人格提示", guard)
        self.assertIn("以下示例只约束语气", guard)

    def test_relationship_mood_for_every_level(self):
        from relationship_state import RELATIONSHIP_STATES

        for state in RELATIONSHIP_STATES:
            self.assertNotEqual(persona_profile.relationship_mood(state["name"]), f"当前心情：{state['name']}。")


class PromptBuilderTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()
        service = self.fx.service
        service.put_state(OWNER_SCOPE, {
            "relationship_level": "非常亲密", "memory_highlights": ["管理员的私密约定"],
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

    def test_group_scene_hides_owner_private_data(self):
        text, _ = self._system_text(qq_group_env(OWNER_QQ))
        self.assertIn(f"QQ 群 {GROUP_ID}", text)
        self.assertIn("开衫", text)
        self.assertNotIn("管理员的私密约定", text)
        self.assertNotIn("管理员的生日是二月五日", text)
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

    def test_owner_qq_scene_is_owner_scope(self):
        text, _ = self._system_text(qq_private_env(OWNER_QQ))
        self.assertIn("管理员正在通过 QQ 私聊找你", text)
        self.assertIn("管理员的生日是二月五日", text)

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
