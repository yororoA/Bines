"""Full QQ prompt -> fake model -> local tool -> simulated send, with no live QQ."""
import copy
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from verification import napcat_integration as napcat
from common.context_budget import ContextTokenCounter
from openai.types.chat import ChatCompletionMessage
from thinking_model_helper import ThinkingModelHelper
from tools.context import ToolContext, tool_context


class FullQQBudgetRegression(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_BACKEND": "embedded",
            "BINES_CONTEXT_TOKENIZER_PATH": "", "BINES_MODEL_CONTEXT_TOKENS": "",
            "BINES_WEBSITE_AUTO_OBSERVE": "false",
        })
        environment.start()
        self.addCleanup(environment.stop)
        napcat.Integration.setUp(self)
        self.addCleanup(napcat.Integration.tearDown, self)

    def exercise(self, *, owner, target, observed=False):
        original = napcat.event("1" if owner else "2", msg_id=target,
                                text="查一下现在的时间，再告诉我。")
        napcat.Integration.insert(self, original)
        rows = self.store.claim("budget-regression", 0, 0)
        bot = napcat.QQThinking(self.store, self.client)
        identity = self.store.identity(original)
        with tool_context(ToolContext(identity, rows[0]["id"], "budget-regression")):
            full_tools = bot.runner.function_tools("QQ")
            full_rules = bot.runner.instructions_for_source("QQ", native=True)
        self.assertGreaterEqual(len(full_tools), 50 if owner else 15)

        replies = [
            ChatCompletionMessage(role="assistant", content=None, tool_calls=[{
                "id": "budget-clock", "type": "function",
                "function": {"name": "get_time", "arguments": "{}"},
            }]),
            ChatCompletionMessage(role="assistant", content="时间已经查到了。"),
        ]
        requests = []

        def complete(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            return SimpleNamespace(choices=[SimpleNamespace(message=replies.pop(0), finish_reason="stop")])

        model = ThinkingModelHelper.__new__(ThinkingModelHelper)
        model.model = "offline-budget-regression"
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        observation = "网页观察（合成数据）：" + "相机样片页面可见说明。" * 1600
        observe = lambda messages: [*messages, {"role": "system", "content": observation}] if observed else messages
        with patch.dict(os.environ, {"BINES_CONTEXT_MAX_TOKENS": str(target)}), patch(
            "thinking_model_helper.with_website_observations", side_effect=observe,
        ), patch("tools.napcat_tool.GatewayClient", return_value=self.client):
            bot.process(rows, model)
        self.store.finish([row["id"] for row in rows], "budget-regression")

        self.assertEqual(len(requests), 2, "the complete task must reach both model turns")
        for request in requests:
            self.assertEqual(request["tools"], full_tools, "budgeting must retain every authorized tool")
            self.assertIn(full_rules, [message.get("content") for message in request["messages"]])
        self.assertTrue(any(message.get("role") == "tool" and message.get("tool_call_id") == "budget-clock"
                            for message in requests[1]["messages"]))
        sends = [(action, params) for action, params in self.api.calls if action == "send_private_msg"]
        self.assertTrue(sends, "the simulated user must receive the completed answer")
        self.assertIn("时间已经查到了", str(sends[-1][1]))
        if observed:
            counter = ContextTokenCounter()
            self.assertGreater(counter.messages(requests[0]["messages"]) + counter.count(full_tools), target)

    def test_existing_32768_setting_runs_complete_owner_and_regular_qq_turns(self):
        for owner in (False, True):
            with self.subTest(owner=owner):
                self.exercise(owner=owner, target=32768)

    def test_default_target_runs_full_owner_tools_with_large_observation(self):
        self.exercise(owner=True, target=65536, observed=True)


if __name__ == "__main__":
    unittest.main()
