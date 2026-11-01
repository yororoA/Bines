"""Timezone and model-boundary regressions; no network or outgoing messages."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from helpers import ROOT_DIR, local_env
from common.clock import CLOCK_MARKER, clock_message, now, with_current_time
from tools.time_tool import get_current_time
import prompt_builder
from thinking_model_helper import ThinkingModelHelper

TS = datetime(2026, 10, 22, 16, 30, tzinfo=timezone.utc).timestamp()


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"BINES_MEMORY_TIMEZONE": "Asia/Shanghai"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_tool_uses_beijing_even_when_os_uses_utc(self):
        try:
            with patch.dict(os.environ, {"TZ": "UTC"}):
                if hasattr(time, "tzset"):
                    time.tzset()
                with patch("common.clock.time.time", return_value=TS):
                    self.assertEqual(get_current_time(),
                        "2026年10月23日 星期五 00:30:00（Asia/Shanghai，UTC+08:00）")
                    self.assertEqual(now().timestamp(), TS)
        finally:
            if hasattr(time, "tzset"):
                time.tzset()

    def test_custom_timezone_keeps_dst_and_calendar_date(self):
        with patch.dict(os.environ, {"BINES_MEMORY_TIMEZONE": "America/New_York"}):
            with patch("common.clock.time.time", return_value=TS):
                self.assertEqual(get_current_time(),
                    "2026年10月22日 星期四 12:30:00（America/New_York，UTC-04:00）")
            winter = datetime(2026, 12, 1, 16, 30, tzinfo=timezone.utc).timestamp()
            self.assertEqual(now(winter).utcoffset().total_seconds(), -5 * 3600)

    def test_current_prompt_uses_live_clock_and_history_keeps_original_day(self):
        previous = TS - 3600
        context = {
            "envelope": local_env(ts=TS - 86400),
            "working": [{"role": "user", "content": "昨晚的消息", "ts": previous}],
            "recalled": [], "profile": [], "state": {},
        }
        with patch("common.clock.time.time", return_value=TS):
            messages = prompt_builder.build_conversation_messages(context)
        clock = next(m for m in messages if m["content"].startswith(CLOCK_MARKER))
        self.assertIn("2026年10月23日", clock["content"])
        historical = next(m for m in messages if m["role"] == "user")
        self.assertIn("2026/10/22 23:30", historical["content"])
        self.assertIn("旧工具回执", clock["content"])
        self.assertIn("current_time", clock["content"])
        self.assertNotIn("当前真实时间", prompt_builder._state_block({"current_time": "1999年1月1日"}))

    def test_refresh_appends_clock_without_mutating_tool_reasoning(self):
        messages = [clock_message(TS - 86400), {"role": "user", "content": "开始"},
                    {"role": "assistant", "content": "", "reasoning_content": "推理",
                     "tool_calls": [{"id": "call"}]},
                    {"role": "tool", "tool_call_id": "call", "content": "回执"}]
        original = deepcopy(messages)
        refreshed = with_current_time(messages, TS)
        self.assertEqual(messages, original)
        self.assertEqual(sum(m["content"].startswith(CLOCK_MARKER) for m in refreshed), 2)
        self.assertEqual(refreshed[:-1], original)
        self.assertIn("2026年10月23日", refreshed[-1]["content"])
        self.assertEqual(with_current_time(refreshed, TS), refreshed)

    def test_time_gap_and_date_retrieval_use_same_calendar(self):
        from message_flow_utils import build_time_gap_instruction
        from memory.retrieval import time_intent
        last = datetime(2026, 10, 22, 15, 50, tzinfo=timezone.utc).timestamp()
        self.assertIn("1 days ago", build_time_gap_instruction(last, "你好", now(TS)))
        date, historical = time_intent("2026/10/23 的记录")
        self.assertEqual(date, datetime(2026, 10, 22, 16, tzinfo=timezone.utc).timestamp())
        self.assertTrue(historical)

    def test_docker_uses_configured_clock_in_all_python_services(self):
        import yaml
        import docker_launcher
        compose = yaml.safe_load((ROOT_DIR / "deploy/docker/compose.yaml").read_text())
        for name in ("chatbot", "qq-thinking", "memory-server", "memory-worker", "rag-web"):
            env = compose["services"][name]["environment"]
            self.assertEqual(env["TZ"], "${BINES_MEMORY_TIMEZONE:-Asia/Shanghai}")
            self.assertEqual(env["TZ"], env["BINES_MEMORY_TIMEZONE"])
        with patch.dict(os.environ, {"BINES_MEMORY_TIMEZONE": "America/New_York"}):
            self.assertEqual(docker_launcher._environment("chatbot")["BINES_MEMORY_TIMEZONE"], "America/New_York")


class ModelClockTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"BINES_MEMORY_TIMEZONE": "Asia/Shanghai"})
        env.start()
        self.addCleanup(env.stop)

    def test_native_qq_clock_refreshes_after_tool_and_preserves_reasoning(self):
        call = SimpleNamespace(
            id="time-call", function=SimpleNamespace(name="get_time", arguments="{}"),
            model_dump=lambda **_: {"id": "time-call", "type": "function",
                                   "function": {"name": "get_time", "arguments": "{}"}},
        )
        responses = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="", tool_calls=[call], reasoning_content="完整保留推理"))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="现在是北京时间零点三十二", tool_calls=[]))]),
        ]
        model = ThinkingModelHelper.__new__(ThinkingModelHelper)
        create = Mock(side_effect=responses)
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        model.model = "test"
        runner = SimpleNamespace(
            instructions_for_source=lambda *_, **__: "工具说明",
            function_tools=lambda *_: [],
            action_to_skill={"get_time": "system-control"},
            execute=Mock(return_value="零点三十"),
        )
        messages = [clock_message(TS - 86400), {"role": "user", "content": "现在几点"}]
        with patch("common.clock.clock_message", side_effect=[clock_message(TS), clock_message(TS + 120)]):
            result, _, _ = model.run_skill_protocol(messages, runner, source="QQ")
        self.assertIn("三十二", result)
        requests = create.call_args_list
        first, second = [request.kwargs["messages"] for request in requests]
        self.assertIn("00:30:00", first[-1]["content"])
        self.assertIn("00:32:00", second[-1]["content"])
        self.assertEqual(second[:len(first)], first)
        assistant = next(m for m in requests[1].kwargs["messages"] if m.get("tool_calls"))
        self.assertEqual(assistant["reasoning_content"], "完整保留推理")
        self.assertEqual(sum(m["role"] == "user" for m in requests[1].kwargs["messages"]), 1)

    def test_desktop_request_refreshes_clock_before_api_call(self):
        from agents import MainAgent
        agent = MainAgent.__new__(MainAgent)
        create = Mock(return_value="response")
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        messages = [clock_message(TS - 86400), {"role": "user", "content": "现在几点"}]
        with patch("common.clock.time.time", return_value=TS):
            agent._post_chat_completion({"messages": messages, "model": "test"})
        self.assertIn("00:30:00", create.call_args.kwargs["messages"][-1]["content"])
        self.assertEqual(messages[0], clock_message(TS - 86400))

    def test_proactive_decision_has_human_readable_scheduler_time(self):
        from common.proactive import _complete
        model = SimpleNamespace(complete_text=Mock(return_value="{}"))
        _complete("评估自主意愿", {"now": TS}, model=model)
        messages = model.complete_text.call_args.args[0]
        self.assertEqual(messages[0]["content"], "评估自主意愿")
        self.assertIn("2026年10月23日 星期五 00:30:00", messages[1]["content"])
        self.assertEqual(json.loads(messages[-1]["content"])["now"], TS)

    def test_memory_completion_keeps_its_historical_materials(self):
        # Clock refresh belongs to conversational/skill requests. Historical
        # summarization/diary completion retains the supplied evidence date.
        from test_model_completion import model_with_responses, response
        messages = [{"role": "user", "content": "2020年日记材料"}]
        model = model_with_responses(response("2020年的日记"))
        self.assertEqual(model.complete_text(messages), "2020年的日记")
        self.assertEqual(model.client.chat.completions.create.call_args.kwargs["messages"], messages)


if __name__ == "__main__":
    unittest.main()
