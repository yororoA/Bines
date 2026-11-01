"""Activity request boundaries with fake models, tools and monotonic time."""
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from chatBot.messages import LOCAL_IDENTITY
from common.activity import activity_checkpoint
from prompt_builder import empty_context
from test_model_completion import model_with_responses, response
from tools.context import ToolContext, tool_context
from will_runtime import ActivityYield, WillSkillRunner, run_intention


def native_call(number, arguments="{}"):
    data = {"id": str(number), "type": "function",
            "function": {"name": "get_time", "arguments": arguments}}
    return SimpleNamespace(id=str(number), function=SimpleNamespace(**data["function"]),
                           model_dump=lambda **_: data)


class ActivityRequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        for patcher in (
            patch.dict(os.environ, {"BINES_ACTIVITY_QUANTUM_SECONDS": "90",
                                    "BINES_ACTIVITY_QUANTUM_ACTIONS": "24",
                                    "BINES_WEBSITE_AUTO_OBSERVE": "false",
                                    "BINES_CONTEXT_MAX_TOKENS": "65536",
                                    "BINES_MODEL_CONTEXT_TOKENS": "",
                                    "BINES_CONTEXT_TOKENIZER_PATH": ""}),
            patch("will_runtime.time.monotonic", side_effect=lambda: self.now),
            patch("common.model_usage.record_usage"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runner = SimpleNamespace(
            instructions_for_source=lambda *_, **__: "fixture tool rules",
            function_tools=lambda *_, **__: [],
            action_to_skill={"get_time": "system-control"},
            execute=Mock(return_value=json.dumps({"status": "ok", "result": "saved_fixture_result"})),
            parse_model_command=Mock(return_value=None),
        )
        self.candidate = {"id": "fixture-activity", "kind": "curiosity", "topic": "fixture",
                          "intent": "read fixture", "reason": "offline test", "contact_allowed": True}
        self.memory = empty_context({"scope": "owner", "channel_type": "qq_private"})

    def model(self, steps):
        model = model_with_responses()
        steps = iter(steps)
        model.request_starts = []

        def create(**_):
            duration, result = next(steps)
            model.request_starts.append(self.now)
            self.now += duration
            return result

        model.client.chat.completions.create.side_effect = create
        return model

    def run_batch(self, model, *, candidate=None, check=lambda: "", source="QQ"):
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        with tool_context(context):
            text, outcomes = run_intention(model, self.runner, self.memory,
                                          candidate or self.candidate, check, source=source)
        return context, text, outcomes

    def test_first_request_and_within_batch_next_request_are_allowed(self):
        model = self.model([(10, response(tool_calls=[native_call(1)])), (10, response("done"))])
        _, text, outcomes = self.run_batch(model)
        self.assertEqual((text, model.request_starts, len(outcomes)), ("done", [0, 10], 1))
        self.runner.execute.assert_called_once()

    def test_slow_response_keeps_its_action_then_yields_before_next_request_and_resumes(self):
        model = self.model([(100, response(tool_calls=[native_call(1)]))])
        context, text, outcomes = self.run_batch(model)
        self.assertEqual(model.request_starts, [0])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(context.activity["mode"], "review")
        saved = activity_checkpoint(self.candidate, context.activity, outcomes, text)
        self.assertEqual(saved["sequence"], 1)
        self.assertIn("saved_fixture_result", saved["observations"][0]["result"])

        choice = {"mode": "continue", "next_intent": "read next fixture", "topic": "fixture",
                  "reason": "use saved progress", "delay_seconds": 2}
        next_candidate = {**self.candidate, "activity": saved}
        next_model = self.model([(10, response(tool_calls=[native_call(2)])), (10, response("done"))])
        with patch("common.activity_deliberation.choose_activity", return_value=choice) as choose:
            next_context, next_text, next_outcomes = self.run_batch(next_model, candidate=next_candidate)
        self.assertEqual(choose.call_args.args[2]["activity"], saved)
        self.assertEqual(next_model.request_starts, [100, 110])
        self.assertEqual(len(next_outcomes), 1)
        self.assertEqual(self.runner.execute.call_count, 2)  # No replay of the first batch's action.
        next_saved = activity_checkpoint(next_candidate, next_context.activity, next_outcomes, next_text)
        self.assertEqual((next_saved["id"], next_saved["sequence"]), (saved["id"], 2))

    def test_invalid_arguments_cannot_bypass_time_limit_without_any_outcomes(self):
        model = self.model([(10, response(tool_calls=[native_call(i, "{")])) for i in range(25)])
        context, _, outcomes = self.run_batch(model)
        self.assertEqual(model.request_starts, list(range(0, 90, 10)))
        self.assertEqual(outcomes, [])
        self.assertEqual(context.activity["mode"], "review")
        self.runner.execute.assert_not_called()

    def test_native_length_retry_is_not_sent_after_time_limit_and_keeps_prior_outcomes(self):
        model = self.model([(40, response(tool_calls=[native_call(1)])),
                            (50, response("partial", finish_reason="length", tool_calls=[native_call(2)]))])
        context, _, outcomes = self.run_batch(model)
        self.assertEqual(model.request_starts, [0, 40])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(context.activity["observations"], outcomes)
        self.assertEqual(context.activity["mode"], "review")
        self.runner.execute.assert_called_once()  # The truncated response's call never ran.

    def test_native_length_retry_is_allowed_while_time_remains(self):
        model = self.model([(10, response("partial", finish_reason="length")), (10, response("done"))])
        _, text, outcomes = self.run_batch(model)
        self.assertEqual((model.request_starts, text, outcomes), ([0, 10], "done", []))

    def test_cancellation_precedes_automatic_review_even_at_expired_boundary(self):
        check = Mock(return_value="incoming_user_message")
        wrapper = WillSkillRunner(self.runner, self.candidate, check)
        context = ToolContext(LOCAL_IDENTITY, autonomous=True)
        self.now = 100
        model = self.model([])
        with tool_context(context), self.assertRaisesRegex(RuntimeError, "incoming_user_message"):
            model.run_tool_calls([], wrapper)
        self.assertEqual(context.activity, {})
        model.client.chat.completions.create.assert_not_called()
        check.assert_called_once()

    def test_existing_control_is_preserved_instead_of_being_rewritten_by_timeout(self):
        for mode in ("continue", "finish", "pause", "review"):
            with self.subTest(mode=mode):
                check = Mock(return_value="cancelled")
                wrapper = WillSkillRunner(self.runner, self.candidate, check)
                self.now += 100
                saved = {"mode": mode, "note": "existing choice", "delay_seconds": 0}
                context = ToolContext(LOCAL_IDENTITY, autonomous=True, activity=dict(saved))
                model = self.model([])
                with tool_context(context), self.assertRaises(ActivityYield):
                    model.run_tool_calls([], wrapper)
                self.assertEqual(context.activity, saved)
                model.client.chat.completions.create.assert_not_called()
                check.assert_not_called()

    def test_ordinary_runner_does_not_acquire_an_activity_time_limit(self):
        model = self.model([(100, response(tool_calls=[native_call(1)])), (100, response("done"))])
        with tool_context(ToolContext(LOCAL_IDENTITY)):
            text, history, _ = model.run_tool_calls([], self.runner)
        self.assertEqual((model.request_starts, text, len(history)), ([0, 100], "done", 1))

    def test_text_protocol_checks_next_request_and_length_retry(self):
        for length in (False, True):
            with self.subTest(length=length):
                self.now = 0
                self.runner.execute.reset_mock()
                self.runner.parse_model_command.return_value = {"skill": "system-control",
                                                               "action": "get_time", "args": {}}
                model = self.model([(100, response("fixture", finish_reason="length" if length else "stop"))])
                context, _, outcomes = self.run_batch(model, source="local")
                self.assertEqual(model.request_starts, [0])
                self.assertEqual(len(outcomes), 0 if length else 1)
                self.assertEqual(context.activity["mode"], "review")


if __name__ == "__main__":
    unittest.main()
