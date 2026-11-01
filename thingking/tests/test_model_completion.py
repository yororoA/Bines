"""Memory completion regressions: thinking budgets, JSON and empty responses."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from helpers import MemoryFixture, local_env
from memory.worker import MemoryWorker
from persona_profile import qq_style_guard
from thinking_model_helper import ModelCompletionError, ThinkingModelHelper


def response(content=None, finish_reason="stop", reasoning=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls),
        )],
        usage=SimpleNamespace(
            completion_tokens=1500,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=1500),
        ),
    )


def model_with_responses(*responses):
    model = ThinkingModelHelper.__new__(ThinkingModelHelper)
    model.model = "test"
    model.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=Mock(side_effect=responses)),
    ))
    return model


class ModelCompletionTest(unittest.TestCase):
    def test_native_truncation_retries_current_request_without_replaying_previous_tool(self):
        call = SimpleNamespace(id="read:1", function=SimpleNamespace(name="browser_search", arguments="{}"),
                               model_dump=lambda **kwargs: {"id": "read:1", "type": "function",
                                   "function": {"name": "browser_search", "arguments": "{}"}})
        model = model_with_responses(
            response(finish_reason="tool_calls", reasoning="读资料", tool_calls=[call]),
            response("截断正文不能采用", finish_reason="length"),
            response("已读到资料"),
        )
        runner = Mock()
        runner.instructions_for_source.return_value = "测试工具"
        runner.function_tools.return_value = []
        runner.action_to_skill = {"browser_search": "web-research"}
        runner.execute.return_value = "真实搜索结果"
        voice = qq_style_guard(state={"relationship_turn_level": "亲近"})
        messages = [
            {"role": "system", "content": voice},
            {"role": "system", "content": "当前会话的工具与消息上下文"},
            {"role": "system", "content": voice},
            {"role": "user", "content": "读资料"},
        ]
        text, _, _ = model.run_tool_calls(messages, runner)
        self.assertEqual(text, "已读到资料")
        self.assertEqual(runner.execute.call_count, 1)
        requests = model.client.chat.completions.create.call_args_list
        self.assertEqual([call.kwargs["max_tokens"] for call in requests], [2048, 2048, 8192])
        self.assertEqual(requests[1].kwargs["messages"], requests[2].kwargs["messages"])
        for request in requests:
            sent = request.kwargs["messages"]
            self.assertEqual(sum(m.get("content") == voice for m in sent), 1)
            self.assertEqual(sent[-1], {"role": "system", "content": voice})
            self.assertIn({"role": "user", "content": "读资料"}, sent)
        continuation = requests[1].kwargs["messages"]
        receipt_index = next(i for i, m in enumerate(continuation) if m.get("tool_call_id") == "read:1")
        self.assertEqual(continuation[receipt_index], {
            "role": "tool", "tool_call_id": "read:1", "content": "真实搜索结果",
        })
        self.assertEqual(continuation[receipt_index - 1]["reasoning_content"], "读资料")
        self.assertLess(receipt_index, len(continuation) - 1)
        self.assertEqual(sum(m.get("content") == voice for m in messages), 2)

    def test_reasoning_only_truncation_retries_original_request_in_json_mode(self):
        model = model_with_responses(
            response(finish_reason="length", reasoning="不能存入记忆的推理"),
            response('{"summary":"一起听歌"}'),
        )
        messages = [{"role": "user", "content": "输出 JSON 摘要"}]
        text = model.complete_text(messages, max_tokens=8192, response_format={"type": "json_object"})
        self.assertEqual(json.loads(text), {"summary": "一起听歌"})
        requests = model.client.chat.completions.create.call_args_list
        self.assertEqual([call.kwargs["max_tokens"] for call in requests], [8192, 16384])
        for call in requests:
            self.assertEqual(call.kwargs["messages"], messages)
            self.assertEqual(call.kwargs["response_format"], {"type": "json_object"})
            self.assertNotIn("extra_body", call.kwargs, "use the configured thinking mode")

    def test_complete_looking_json_with_length_is_not_accepted(self):
        model = model_with_responses(
            response('{"summary":"不完整事件"}', finish_reason="length"),
            response('{"summary":"完整事件"}'),
        )
        self.assertEqual(model.complete_text([], max_tokens=1500), '{"summary":"完整事件"}')

    def test_second_truncation_fails_with_safe_diagnostics(self):
        model = model_with_responses(
            response(finish_reason="length"),
            response("private partial output", finish_reason="length"),
        )
        with self.assertRaises(ModelCompletionError) as caught:
            model.complete_text([], max_tokens=8192)
        self.assertEqual(model.client.chat.completions.create.call_count, 2)
        self.assertIn("finish_reason=length", str(caught.exception))
        self.assertIn("reasoning_tokens=1500", str(caught.exception))
        self.assertNotIn("private partial output", str(caught.exception))

    def test_retry_budget_is_bounded(self):
        model = model_with_responses(response("unfinished", finish_reason="length"))
        with self.assertRaises(ModelCompletionError):
            model.complete_text([], max_tokens=262144)
        self.assertEqual(model.client.chat.completions.create.call_count, 1)

    def test_empty_final_text_and_missing_choices_are_errors(self):
        for result in (response(" \n ", reasoning="reasoning"), SimpleNamespace(choices=[])):
            with self.subTest(result=result):
                model = model_with_responses(result)
                with self.assertRaises(ModelCompletionError):
                    model.complete_text([])
                self.assertEqual(model.client.chat.completions.create.call_count, 1)

    def test_diary_keeps_plain_text_format(self):
        model = model_with_responses(response(" 今天一起听歌。 "))
        self.assertEqual(model.complete_text([]), "今天一起听歌。")
        self.assertNotIn("response_format", model.client.chat.completions.create.call_args.kwargs)

    def test_association_budget_override_is_small_and_keeps_thinking_disabled(self):
        model = model_with_responses(response(finish_reason="length"), response('{"links":[]}'))
        self.assertEqual(model.complete_text([], max_tokens=4096, retry_tokens=8192, thinking="disabled",
                                            timeout=90), '{"links":[]}')
        requests = model.client.chat.completions.create.call_args_list
        self.assertEqual([r.kwargs["max_tokens"] for r in requests], [4096, 8192])
        self.assertTrue(all(r.kwargs["extra_body"] == {"thinking": {"type": "disabled"}} for r in requests))
        self.assertTrue(all(r.kwargs["timeout"] == 90 for r in requests))

    def test_association_route_cannot_inherit_summary_retry_budget(self):
        from thingking.consolidation_worker import build_worker
        model = SimpleNamespace(complete_text=Mock(return_value='{"links":[]}'))
        worker = build_worker(Mock(), model)
        worker.association_complete_text([])
        request = model.complete_text.call_args.kwargs
        self.assertEqual(request["max_tokens"], 4096)
        self.assertEqual(request["retry_tokens"], 8192)
        self.assertEqual(request["max_retries"], 0)
        self.assertEqual(request["thinking"], "disabled")

    def test_worker_routes_json_tasks_separately_from_diary(self):
        from thingking.consolidation_worker import build_worker
        model = SimpleNamespace(complete_text=Mock(return_value="{}"))
        worker = build_worker(Mock(), model)
        messages = [{"role": "user", "content": "JSON"}]
        worker.summary_complete_text(messages)
        worker.state_complete_text(messages)
        worker.complete_text(messages)
        summary, state, diary = model.complete_text.call_args_list
        self.assertEqual(summary.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(state.kwargs["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", diary.kwargs)
        self.assertGreaterEqual(summary.kwargs["max_tokens"], 8192)
        self.assertGreaterEqual(state.kwargs["max_tokens"], 4096)


class MemoryCompletionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture(summarize_trigger=6, summarize_chunk=6, state_updates=True)

    def tearDown(self):
        self.fx.close()

    def test_truncated_summary_recovery_commits_only_final_answer_once(self):
        service = self.fx.service
        for i in range(3):
            self.fx.clock.advance(60)
            service.record_turn(local_env(ts=self.fx.clock()), f"听歌{i}", f"一起听{i}")
        model = model_with_responses(
            response('{"summary":"推测内容', finish_reason="length"),
            response(json.dumps({"summary": "管理员和助手一起听歌。", "tags": ["Daily"], "facts": []})),
        )
        worker = MemoryWorker(service, model.complete_text, lambda: "")
        self.assertTrue(worker.run_once())
        episodes = self.fx.repo.list_memories(kinds=["episode"])
        self.assertEqual([e["content"] for e in episodes], ["管理员和助手一起听歌。"])
        self.assertEqual(self.fx.repo.pending_message_count("local"), 0)
        job = self.fx.repo.conn.execute("SELECT status, attempts FROM jobs WHERE kind='summarize'").fetchone()
        self.assertEqual(tuple(job), ("done", 1))

    def test_empty_summary_keeps_original_messages_and_requeues_job(self):
        service = self.fx.service
        for i in range(3):
            self.fx.clock.advance(60)
            service.record_turn(local_env(ts=self.fx.clock()), f"听歌{i}", f"一起听{i}")
        model = model_with_responses(response(reasoning="推理文本"))
        worker = MemoryWorker(service, model.complete_text, lambda: "")
        self.assertTrue(worker.run_once())
        self.assertEqual(self.fx.repo.count_memories(kinds=["episode", "semantic"]), 0)
        self.assertEqual(self.fx.repo.pending_message_count("local"), 6)
        job = self.fx.repo.conn.execute("SELECT status, last_error FROM jobs WHERE kind='summarize'").fetchone()
        self.assertEqual(job["status"], "pending")
        self.assertIn("empty text", job["last_error"])

    def test_empty_state_is_retried_without_marking_no_change(self):
        service = self.fx.service
        service.record_turn(local_env(ts=self.fx.clock()), "谢谢你关心我", "别客气")
        before = service.get_state("owner")
        worker = MemoryWorker(service, lambda _: "", lambda: "", state_complete_text=lambda _: "")
        self.assertTrue(worker.run_once())
        self.assertEqual(service.get_state("owner"), before)
        job = self.fx.repo.conn.execute("SELECT status, last_error FROM jobs WHERE kind='update_state'").fetchone()
        self.assertEqual(job["status"], "pending")
        self.assertIn("state model returned empty text", job["last_error"])

    def test_explicit_empty_json_state_can_complete_as_no_change(self):
        service = self.fx.service
        service.record_turn(local_env(ts=self.fx.clock()), "你好", "嗨")
        before = service.get_state("owner")
        worker = MemoryWorker(service, lambda _: "", lambda: "", state_complete_text=lambda _: "{}")
        self.assertTrue(worker.run_once())
        self.assertEqual(service.get_state("owner"), before)
        job = self.fx.repo.conn.execute("SELECT status FROM jobs WHERE kind='update_state'").fetchone()
        self.assertEqual(job["status"], "done")


if __name__ == "__main__":
    unittest.main()
