"""Desktop runtime regressions using fake streams, queues and tools only."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import helpers
from agents import MainAgent
from common.memory_journal import outcome_status, tool_failure
from interrupt_flow import DialogueQueue, submit_pending_interrupt
from skill_runner import SkillRunner
from thinking_model_helper import ModelCompletionError
from thinking_stream_runner import run_main_agent_rounds
from test_model_completion import model_with_responses, response
from tools import TOOLS_REGISTRY


def setUpModule():
    # Fake model calls must never discover the developer's live browser bridge.
    env = patch.dict(os.environ, {"BINES_WEBSITE_AUTO_OBSERVE": "false"})
    env.start()
    unittest.addModuleCleanup(env.stop)


def chunk(text="", finish_reason=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=text), finish_reason=finish_reason,
    )])


def agent_with_stream(stream):
    agent = MainAgent.__new__(MainAgent)
    agent.model = "test"
    agent.skill_instructions = ""
    agent._post_chat_completion = Mock(return_value=stream)
    return agent


def run_stream(agent, *, runner=None, interrupt=lambda: False, send=None):
    return run_main_agent_rounds(
        messages=[{"role": "user", "content": "测试"}], main_agent=agent,
        skill_runner=runner or Mock(), zmq_send=send or Mock(),
        is_only_action_or_empty=lambda text: not text.strip(), enable_audio=False,
        source="MANUAL", interrupt_requested=interrupt, max_skill_rounds=1,
    )


class StreamReliabilityTests(unittest.TestCase):
    def test_holder_interruption_is_not_reported_as_completed_or_spoken(self):
        holder, send = {}, Mock()

        def generate():
            yield "尚未讲完的半句话"
            holder["interrupted"] = True

        agent = Mock()
        agent.run_one_turn_streaming.return_value = generate(), holder
        result = run_stream(agent, send=send)
        self.assertTrue(result["exited_due_to_interrupt"])
        self.assertEqual(result["full_raw_response"], "")
        self.assertEqual(result["error"], "")
        send.assert_not_called()

    def test_interruption_before_request_makes_no_model_call(self):
        agent, send = Mock(), Mock()
        result = run_stream(agent, interrupt=lambda: True, send=send)
        self.assertTrue(result["exited_due_to_interrupt"])
        agent.run_one_turn_streaming.assert_not_called()
        send.assert_not_called()

    def test_midstream_exception_keeps_only_already_emitted_sentence(self):
        emitted = threading.Event()
        closed = threading.Event()

        def generate():
            try:
                yield "已经说完。  "
                if not emitted.wait(2):
                    raise AssertionError("consumer did not emit the first sentence")
                yield "尚未说完"
                raise OSError("fake stream disconnected")
            finally:
                closed.set()

        agent = Mock()
        agent.run_one_turn_streaming.return_value = generate(), {}
        sent = []

        def send(text, **kwargs):
            sent.append(text)
            if text == "已经说完。":
                emitted.set()

        result = run_stream(agent, send=send)
        self.assertEqual(result["full_raw_response"], "已经说完。")
        self.assertIn("disconnected", result["error"])
        self.assertNotIn("尚未说完", sent)
        self.assertTrue(closed.is_set())
        self.assertFalse(result["stream_cleanup_pending"])

    def test_incomplete_skill_stream_is_never_dispatched(self):
        def generate():
            yield '[[SKILL_CALL]]{"skill":"x","action":"write","args":{}}[[/SKILL_CALL]]'
            raise OSError("incomplete response")

        runner, agent, send = Mock(), Mock(), Mock()
        agent.run_one_turn_streaming.return_value = generate(), {}
        result = run_stream(agent, runner=runner, send=send)
        self.assertTrue(result["error"])
        self.assertEqual(result["full_raw_response"], "")
        runner.execute.assert_not_called()
        self.assertNotIn("SKILL_CALL", str(send.call_args_list))

    def test_cancel_closes_blocked_response_and_joins_producer(self):
        entered, closed, cancelled = threading.Event(), threading.Event(), threading.Event()

        class Stream:
            close_count = 0

            def __iter__(self):
                entered.set()
                if not closed.wait(3):
                    raise AssertionError("response was not closed on cancellation")
                raise OSError("closed")
                yield  # Make this a streaming iterator.

            def close(self):
                self.close_count += 1
                closed.set()

        stream = Stream()
        agent = agent_with_stream(stream)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_stream, agent, interrupt=cancelled.is_set)
            self.assertTrue(entered.wait(2))
            cancelled.set()
            result = future.result(timeout=3)
        self.assertTrue(result["exited_due_to_interrupt"])
        self.assertFalse(result["stream_cleanup_pending"])
        self.assertEqual(stream.close_count, 1)

    def test_cancel_while_connection_opens_closes_late_response(self):
        opening, release, closed, cancelled = (threading.Event() for _ in range(4))
        stream = Mock()
        stream.close.side_effect = closed.set
        agent = agent_with_stream(stream)

        def open_request(*args, **kwargs):
            opening.set()
            if not release.wait(4):
                raise AssertionError("test never released fake connection")
            return stream

        agent._post_chat_completion.side_effect = open_request
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_stream, agent, interrupt=cancelled.is_set)
            self.assertTrue(opening.wait(2))
            cancelled.set()
            try:
                result = future.result(timeout=3)
                self.assertTrue(result["exited_due_to_interrupt"])
                self.assertTrue(result["stream_cleanup_pending"])
            finally:
                release.set()
            self.assertTrue(closed.wait(2))
        stream.close.assert_called_once()

    def test_normal_response_closes_once_without_spurious_interruption(self):
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([chunk("完整回答"), chunk(finish_reason="stop")]))
        send = Mock()
        result = run_stream(agent_with_stream(stream), send=send)
        self.assertFalse(result["exited_due_to_interrupt"])
        self.assertFalse(result["error"])
        self.assertFalse(result["stream_cleanup_pending"])
        self.assertEqual(result["full_raw_response"], "完整回答")
        stream.close.assert_called_once()
        self.assertEqual(send.call_args.args[0], "完整回答")

    def test_length_finish_does_not_accept_complete_looking_partial_answer(self):
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([chunk("看起来完整。"), chunk(finish_reason="length")]))
        result = run_stream(agent_with_stream(stream))
        self.assertIn("未完成", result["error"])
        self.assertEqual(result["full_raw_response"], "")
        stream.close.assert_called_once()

    def test_eof_without_finish_marker_is_incomplete(self):
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([chunk("回答突然中断")]))
        result = run_stream(agent_with_stream(stream))
        self.assertIn("缺少完成标记", result["error"])
        self.assertEqual(result["full_raw_response"], "")
        stream.close.assert_called_once()

    def test_producer_preserves_turn_context(self):
        turn = ContextVar("test_turn", default="missing")
        token = turn.set("owner-turn")
        observed = []
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([chunk("完成"), chunk(finish_reason="stop")]))
        agent = agent_with_stream(stream)
        agent._post_chat_completion.side_effect = lambda *args, **kwargs: observed.append(turn.get()) or stream
        try:
            run_stream(agent)
        finally:
            turn.reset(token)
        self.assertEqual(observed, ["owner-turn"])

    def test_desktop_request_injects_fresh_website_observation_without_mutating_input(self):
        agent = MainAgent.__new__(MainAgent)
        agent.client = Mock()
        messages = [{"role": "user", "content": "看看网页"}]
        observation = {"role": "system", "content": "fresh observed page"}
        with patch("agents.with_website_observations", side_effect=lambda rows: [*rows, observation]) as observe:
            agent._post_chat_completion({"messages": messages, "model": "test"}, stream=True)
        observe.assert_called_once_with(messages)
        self.assertIn(observation, agent.client.chat.completions.create.call_args.kwargs["messages"])
        self.assertEqual(messages, [{"role": "user", "content": "看看网页"}])


class QueueReliabilityTests(unittest.TestCase):
    def test_burst_before_worker_starts_is_fifo_and_schedules_one_drain(self):
        executor = Mock()
        dispatcher = DialogueQueue(executor)
        seen = []
        for index in range(30):
            self.assertTrue(dispatcher.submit(seen.append, index, interrupt=True))
        executor.submit.assert_called_once()
        executor.submit.call_args.args[0]()
        self.assertEqual(seen, list(range(30)))
        self.assertFalse(dispatcher.busy)

    def test_busy_input_interrupts_then_all_accepted_messages_run_once(self):
        active, release, interrupted = (threading.Event() for _ in range(3))
        seen, starts = [], []

        def first():
            active.set()
            if not release.wait(3):
                raise AssertionError("test did not release first dialogue")
            seen.append("first")

        with ThreadPoolExecutor(max_workers=4) as executor:
            dispatcher = DialogueQueue(executor, on_start=lambda: starts.append(len(seen)),
                                       on_interrupt=interrupted.set)
            dispatcher.submit(first)
            self.assertTrue(active.wait(2))
            for index in range(20):
                dispatcher.submit(seen.append, index, interrupt=True)
            self.assertTrue(interrupted.is_set())
            self.assertFalse(dispatcher.submit(seen.append, "automatic", background=True))
            release.set()
        self.assertEqual(seen, ["first", *range(20)])
        self.assertEqual(len(starts), 21)
        self.assertFalse(dispatcher.busy)

    def test_failed_turn_does_not_discard_next_accepted_turn(self):
        executor, seen = Mock(), []
        dispatcher = DialogueQueue(executor)
        dispatcher.submit(Mock(side_effect=RuntimeError("fake turn failure")))
        dispatcher.submit(seen.append, "next")
        with self.assertLogs("bines.thinking.queue", level="ERROR"):
            executor.submit.call_args.args[0]()
        self.assertEqual(seen, ["next"])
        self.assertFalse(dispatcher.busy)

    def test_legacy_pending_handoff_does_not_drop_busy_input(self):
        executor, handler = Mock(), Mock()
        state, lock = {"is_processing": True}, threading.Lock()
        submit_pending_interrupt(pending=("next", "", "MANUAL"), executor=executor,
                                 processing_lock=lock, processing_state=state, process_message=handler)
        executor.submit.call_args.args[0]()
        handler.assert_called_once_with("next", "", source="MANUAL", extra_data=None)
        self.assertFalse(state["is_processing"])


class ToolReceiptTests(unittest.TestCase):
    def test_legacy_failures_do_not_count_as_successful_autonomous_actions(self):
        from will_runtime import outcome_succeeded
        for text in ("失败: 未安装 uiautomation 库", " 执行出错(UIA Thread): 控件不存在",
                     "执行出错(Manual COM): 错误", "Error executing 'test': failed"):
            with self.subTest(text=text):
                self.assertEqual(outcome_status(text), "failed")
                self.assertFalse(outcome_succeeded({"result": text}))

    def test_explicit_receipts_and_boolean_failures_are_respected(self):
        for value in ({"success": False}, {"ok": False}, {"error": "unavailable"}, False):
            self.assertEqual(outcome_status(value), "failed")
        self.assertEqual(outcome_status({"status": "interrupted"}), "interrupted")
        self.assertEqual(outcome_status({"status": "uncertain", "error": "lost receipt"}), "uncertain")
        self.assertEqual(outcome_status({"status": "ok", "message": "没有错误"}), "succeeded")
        self.assertEqual(outcome_status("已经开始播放"), "succeeded")

    def test_music_guard_returns_structured_failure_without_importing_desktop_drivers(self):
        tree = ast.parse((helpers.ROOT_DIR / "tools" / "music_tool.py").read_text())
        decorator = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "require_uia")
        import functools
        namespace = {"functools": functools, "UI_AUTOMATION_AVAILABLE": False, "tool_failure": tool_failure}
        exec(compile(ast.Module(body=[decorator], type_ignores=[]), "music_guard", "exec"), namespace)
        action = Mock()
        receipt = namespace["require_uia"](action)()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["code"], "capability_unavailable")
        action.assert_not_called()

    def test_dispatch_failures_are_structured_and_do_not_repeat_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "SKILL.md").write_text("---\nname: demo\ndescription: test\nactions: run\n---\ntest")
            action = Mock(spec=lambda: None, side_effect=RuntimeError("fake failure"))
            action.__signature__ = __import__("inspect").signature(lambda: None)
            runner = SkillRunner({"run": action}, skills_dir=path)
            receipt = json.loads(runner.execute({"skill": "demo", "action": "run", "args": {}}))
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(outcome_status(receipt), "failed")
            action.assert_called_once()


class PlatformCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.runner = SkillRunner(TOOLS_REGISTRY, skills_dir=helpers.ROOT_DIR / "skills")
        self.music = {name for name, function in TOOLS_REGISTRY.items() if function.module == "music_tool"}

    def test_non_windows_music_is_hidden_and_blocked_before_import(self):
        with patch("skill_runner.sys.platform", "darwin"):
            self.assertFalse(self.music & self.runner.actions_for_source())
            self.assertNotIn("open_netease_music", {row["function"]["name"] for row in self.runner.function_tools()})
            action = next(iter(self.music))
            with patch.object(TOOLS_REGISTRY[action], "resolve") as resolve:
                receipt = json.loads(self.runner.execute({"skill": self.runner.action_to_skill[action],
                                                         "action": action, "args": {}}))
            self.assertEqual(receipt["code"], "capability_unavailable")
            resolve.assert_not_called()

    def test_windows_only_exposes_music_when_uia_dependency_is_present(self):
        with patch("skill_runner.sys.platform", "win32"), patch("skill_runner.importlib.util.find_spec", return_value=None):
            self.assertFalse(self.music & self.runner.actions_for_source())
        with patch("skill_runner.sys.platform", "win32"), patch("skill_runner.importlib.util.find_spec", return_value=object()):
            self.assertTrue(self.music <= self.runner.actions_for_source())


class RetryBudgetTests(unittest.TestCase):
    def test_default_text_expansion_stays_within_configured_ceiling(self):
        with patch.dict(os.environ, {"BINES_MODEL_RETRY_MAX_TOKENS": "10000"}):
            model = model_with_responses(response(finish_reason="length"), response("完成"))
            self.assertEqual(model.complete_text([], max_tokens=8192, retry_tokens=262144), "完成")
        self.assertEqual([call.kwargs["max_tokens"] for call in model.client.chat.completions.create.call_args_list],
                         [8192, 10000])

    def test_no_larger_budget_fails_without_repeating_request(self):
        with patch.dict(os.environ, {"BINES_MODEL_RETRY_MAX_TOKENS": "2048"}):
            model = model_with_responses(response("partial", finish_reason="length"))
            with self.assertRaises(ModelCompletionError):
                model.complete_text([], max_tokens=2048)
        model.client.chat.completions.create.assert_called_once()

    def test_invalid_ceiling_fails_before_model_request(self):
        for value in ("0", "-1", "nan", "262145"):
            with self.subTest(value=value), patch.dict(os.environ, {"BINES_MODEL_RETRY_MAX_TOKENS": value}):
                model = model_with_responses(response("unused"))
                with self.assertRaises(ValueError):
                    model.complete_text([])
                model.client.chat.completions.create.assert_not_called()

    def test_native_truncated_tools_never_execute_even_at_budget_limit(self):
        runner = Mock()
        runner.instructions_for_source.return_value = "tools"
        runner.function_tools.return_value = []
        model = model_with_responses(response(finish_reason="length"))
        with patch.dict(os.environ, {"BINES_MODEL_RETRY_MAX_TOKENS": "2048"}):
            with self.assertRaises(ModelCompletionError):
                model.run_tool_calls([], runner, max_tokens=2048)
        runner.execute.assert_not_called()
        model.client.chat.completions.create.assert_called_once()


class TurnLeaseGuardTests(unittest.TestCase):
    def make_runner(self, registry):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        (path / "SKILL.md").write_text(
            "---\nname: demo\ndescription: test\nactions: " + ", ".join(registry) + "\n---\ntest"
        )
        return SkillRunner(registry, skills_dir=path), path

    def test_lost_lease_is_checked_after_durable_journal_and_before_action(self):
        from common.memory_journal import MemoryJournal, MemoryOutbox
        from tools.context import ToolContext, tool_context
        executed = []
        runner, path = self.make_runner({"browser_search": lambda: executed.append("called")})
        outbox = MemoryOutbox(path / "outbox.db")
        journal = MemoryJournal(outbox, {}, "lease-test")
        observed = []

        def check():
            with outbox.connection() as db:
                records = [json.loads(row[0])["record"] for row in db.execute("SELECT params FROM memory_outbox")]
            observed.extend(row for row in records if row.get("action") == "browser_search")
            return "lease_lost"

        with tool_context(ToolContext(event_id="event", turn_id="turn", check=check, journal=journal)):
            with self.assertRaisesRegex(RuntimeError, "lease_lost"):
                runner.execute({"skill": "demo", "action": "browser_search", "args": {}}, source="QQ")
        self.assertEqual(executed, [])
        self.assertEqual(observed[0]["status"], "started")

    def test_lease_lost_during_lazy_import_prevents_real_action(self):
        from tools.context import ToolContext, tool_context
        expired, executed = [], []

        class Lazy:
            def resolve(self):
                expired.append(True)
                return lambda: None

            def __call__(self):
                executed.append(True)

        runner, _ = self.make_runner({"browser_search": Lazy()})
        with tool_context(ToolContext(event_id="event", check=lambda: "lease_lost" if expired else "")):
            with self.assertRaisesRegex(RuntimeError, "lease_lost"):
                runner.execute({"skill": "demo", "action": "browser_search", "args": {}}, source="QQ")
        self.assertEqual(executed, [])

    def test_native_batch_stops_before_second_action_or_another_model_request(self):
        from tools.context import ToolContext, tool_context
        expired, executed = [], []

        def first():
            executed.append("first")
            expired.append(True)
            return {"status": "ok"}

        def second():
            executed.append("second")

        runner, _ = self.make_runner({"browser_search": first, "web_fetch": second})
        calls = []
        for index, name in enumerate(("browser_search", "web_fetch")):
            value = {"id": str(index), "type": "function", "function": {"name": name, "arguments": "{}"}}
            calls.append(SimpleNamespace(id=str(index), function=SimpleNamespace(name=name, arguments="{}"),
                                         model_dump=lambda _value=value, **kwargs: _value))
        model = model_with_responses(response(tool_calls=calls))
        with tool_context(ToolContext(event_id="event", check=lambda: "lease_lost" if expired else "")):
            with self.assertRaisesRegex(RuntimeError, "lease_lost"):
                model.run_tool_calls([], runner)
        self.assertEqual(executed, ["first"])
        model.client.chat.completions.create.assert_called_once()

    def test_ordinary_gateway_write_checks_lease_before_request_and_receipt(self):
        from chatBot.gateway_client import GatewayClient
        from tools.context import ToolContext, tool_context
        client = GatewayClient.__new__(GatewayClient)
        client.request = Mock()
        for check in (Mock(return_value="lease_lost"), Mock(side_effect=["", "lease_lost"])):
            context = ToolContext(event_id="event", turn_id="turn", check=check)
            with tool_context(context), self.assertRaisesRegex(RuntimeError, "lease_lost"):
                client.raw_api("send_private_msg", {"user_id": "1", "message": "test"})
            self.assertEqual(context.receipts, [])
        client.request.assert_not_called()

    def test_gateway_cancellation_inside_tool_is_not_swallowed_as_normal_failure(self):
        from chatBot.gateway_client import GatewayClient
        from tools.context import ToolContext, tool_context
        expired = []
        client = GatewayClient.__new__(GatewayClient)
        client.request = Mock()

        def action():
            expired.append(True)
            return client.raw_api("get_friend_list")

        runner, _ = self.make_runner({"browser_search": action})
        with tool_context(ToolContext(event_id="event", check=lambda: "lease_lost" if expired else "")):
            with self.assertRaisesRegex(RuntimeError, "lease_lost"):
                runner.execute({"skill": "demo", "action": "browser_search", "args": {}}, source="QQ")
        client.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
