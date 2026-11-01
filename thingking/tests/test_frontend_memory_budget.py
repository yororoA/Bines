"""Recall-to-request regressions: temporary SQLite, lexical search, fake clients."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from common.context_budget import ContextBudgetExceeded, ContextTokenCounter, bound_request
from common.memory_journal import MemoryJournal, MemoryOutbox
from memory.channels import local_envelope
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from prompt_builder import (_bounded_context, _memory_recall_block, _working_messages,
                            build_conversation_messages, scene_for)
from test_model_completion import model_with_responses, response
from tools.context import CURRENT_CONTEXT, ToolContext, tool_context
from tools.dependencies import deps
from tools.memory_tool import read_memory_source, search_memory


class MemoryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="bines-frontend-budget-")
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"BINES_ENV_FILE": "/dev/null", "BINES_CONTEXT_MAX_TOKENS": "32768",
            "BINES_CONTEXT_TOKENIZER_PATH": "", "BINES_MEMORY_DIR": self.tmp.name,
            "BINES_WEBSITE_AUTO_OBSERVE": "false"})
        env.start()
        self.addCleanup(env.stop)
        self.repo = MemoryRepository(":memory:", clock=lambda: 1000.)
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.tmp.name), state_updates=False), clock=lambda: 1000.)
        self.env = self.service.resolve(local_envelope("").to_dict())

    def add_message(self, text, ts=100):
        return self.repo.add_message(self.env.conversation_id, self.env.scope, "user", text, ts, "1970-01-01")

    def budgeted(self, query, available=26000):
        context = self.service.build_context(self.env.to_dict(), query)
        selected, spent = _bounded_context(context, scene_for(self.env.to_dict()), ContextTokenCounter(), available)
        text = _memory_recall_block(selected, record=False) + "\n".join(
            m["content"] for m in _working_messages(selected, scene_for(self.env.to_dict())))
        return context, selected, spent, text

    def test_trimmed_working_window_hit_returns_as_recall(self):
        self.add_message("mars mission acceptance TARGET739", 100)
        for i in range(29):
            self.add_message("unrelated " + "x" * 15000, 101 + i)
        context, selected, _, text = self.budgeted("mars mission acceptance")
        self.assertEqual(len(context["working"]), 30)
        self.assertTrue(context["recall_backfill"])
        self.assertLess(len(selected["working"]), 30)
        self.assertTrue(selected["recalled"])
        self.assertIn("TARGET739", text)

    def test_complete_working_source_is_deduplicated(self):
        self.add_message("mars mission TARGET739")
        context, selected, _, text = self.budgeted("mars mission")
        self.assertTrue(context["recall_backfill"])
        self.assertFalse(selected["recalled"])
        self.assertEqual(text.count("TARGET739"), 1)

    def test_partial_working_source_does_not_hide_answer_at_tail(self):
        self.add_message("background " * 2000 + "mars mission TARGET739")
        _, selected, _, text = self.budgeted("mars mission", available=5000)
        self.assertTrue(selected["working"][0]["_partial"])
        self.assertNotIn("TARGET739", selected["working"][0]["content"])
        self.assertIn("TARGET739", text)

    def test_unused_sections_lend_to_recall_without_exceeding_total(self):
        context = {"recall_query": "needle", "recalled": [
            {"id": str(i), "kind": "episode", "content": "needle " + "证据" * 400}
            for i in range(12)]}
        selected, spent = _bounded_context(context, "local", ContextTokenCounter(), 10000)
        self.assertGreater(spent["recall"], 3000)
        self.assertLessEqual(sum(spent.values()), 10000)
        self.assertGreaterEqual(len(selected["recalled"]), 4)

    def test_lending_preserves_already_complete_unequal_working_entries(self):
        working = [{"id": 1, "role": "user", "content": "short", "ts": 100},
                   {"id": 2, "role": "assistant", "content": "x" * 1800, "ts": 101}]
        context = {"working": working, "profile": [{"id": str(i), "kind": "semantic", "content": "画像" * 1000}
                                                    for i in range(12)]}
        selected, spent = _bounded_context(context, "local", ContextTokenCounter(), 10000)
        self.assertEqual([m["content"] for m in selected["working"]], [m["content"] for m in working])
        self.assertLessEqual(sum(spent.values()), 10000)

    def test_tool_results_are_bounded_and_only_returned_spans_are_presented(self):
        for i in range(20):
            self.repo.add_memory("episode", "owner", "needle " + str(i) + "x" * 5000)
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "outbox.db"), self.env.to_dict(), "tool-test")
        with tool_context(ToolContext(journal=journal)), patch.object(deps, "memory_client", self.service):
            hits = search_memory("needle", 20)
        self.assertTrue(hits)
        self.assertLessEqual(ContextTokenCounter().count(json.dumps(hits, ensure_ascii=False)), 8192)
        self.assertTrue(all(row["is_excerpt"] for row in hits))
        self.assertEqual(len(journal.memory_presentations), len(hits))
        self.assertTrue(all(len(span) <= 700 for row in journal.memory_presentations.values() for span in row["spans"]))
        self.assertIn("retrieval", journal.memory_diagnostics["tool"][0])

    def test_source_paging_is_scoped_and_token_bounded(self):
        original = "needle " + "中文" * 200
        mid = self.repo.add_memory("episode", "owner", original)
        node = self.service.graph.register_memory(mid)
        self.assertIsNone(self.service.read_memory_source(node["id"], "qq:other:private:user"))
        chunks, offset = [], 0
        with patch.object(deps, "memory_client", self.service), patch.dict(os.environ, {"BINES_CONTEXT_MAX_TOKENS": "2048"}):
            for _ in range(30):
                page = read_memory_source(node["id"], offset, 1024)
                self.assertLessEqual(ContextTokenCounter().count(json.dumps(page, ensure_ascii=False)), 512)
                chunks.append(page["content"])
                if page["next_offset"] is None:
                    break
                self.assertGreater(page["next_offset"], offset)
                offset = page["next_offset"]
        self.assertEqual("".join(chunks), original)

    def test_chinese_tool_excerpt_keeps_query_after_token_budgeting(self):
        from tools.memory_tool import _snippet
        text = "无关背景" * 1000 + "火星计划 TARGET739" + "其他内容" * 1000
        span = _snippet(text, "火星计划 TARGET739", ContextTokenCounter(), 256)
        self.assertLessEqual(ContextTokenCounter().count(span), 256)
        self.assertIn("TARGET739", span)

    def test_tool_uses_vector_located_span_without_literal_query_overlap(self):
        prefix = "无关背景。" * 2000
        answer = "去年送给我的纪念物是一条蓝色围巾。"
        hit = {"id": "fixture", "citation_id": "source", "node_id": "source", "node_digest": "digest",
               "kind": "episode", "content": prefix + answer, "matched_spans": [answer], "evidence": []}
        client = SimpleNamespace(search_with_trace=Mock(return_value={"hits": [hit], "trace": {"vector": {"status": "ok"}}}))
        with patch.object(deps, "memory_client", client):
            result = search_memory("cerulean keepsake")
        self.assertEqual(result[0]["content"], answer)
        self.assertEqual(result[0]["expand"]["offset"], len(prefix))

    def test_gateway_forces_page_and_trace_scopes(self):
        from chatBot.memory_hub import MemoryHub
        from chatBot.messages import Identity
        hub = MemoryHub.__new__(MemoryHub)
        hub._call = Mock(return_value=None)
        identity = Identity("account", "user")
        hub.call(identity, "read_memory_source", {"scope": "owner", "source_id": "a", "limit": 10})
        self.assertEqual(hub._call.call_args.kwargs["scope"], identity.scope)
        hub.call(identity, "search_with_trace", {"scopes": ["owner"], "query": "test", "k": 50})
        self.assertEqual(hub._call.call_args.kwargs["scopes"], [identity.scope])
        self.assertEqual(hub._call.call_args.kwargs["k"], 20)

    def test_trace_survives_profile_search_and_turn_record(self):
        self.repo.add_memory("episode", "owner", "needle TARGET739", conversation_id="other")
        context = self.service.build_context(self.env.to_dict(), "needle")
        self.assertGreater(context["retrieval_trace"]["recall"]["candidates"], 0)
        self.assertEqual(context["retrieval_trace"]["profile"]["candidates"], 0)
        outbox = MemoryOutbox(Path(self.tmp.name) / "trace.db")
        journal = MemoryJournal(outbox, self.env.to_dict(), "trace-test")
        with tool_context(ToolContext(journal=journal)):
            build_conversation_messages(context)
            bound_request([{"role": "user", "content": "test"}])
            context["retrieval_trace"]["recall"]["candidates"] = 999
        with outbox.connection() as db:
            record = json.loads(db.execute("SELECT params FROM memory_outbox").fetchone()[0])["record"]
        diag = record["memory_diagnostics"]
        self.assertNotEqual(diag["retrieval"]["recall"]["candidates"], 999)
        self.assertIn("recall_shown", diag["presentation"])
        self.assertEqual(len(diag["request"]), 1)

    def test_request_schema_overhead_rebuilds_memory_before_deduplication(self):
        # First assembly fits all working, so the source is legitimately deduped.
        # Real tool overhead then shortens that window; the source must return.
        self.add_message("mars mission TARGET739", 100)
        for i in range(10):
            self.add_message("other " + "x" * 1000, 101 + i)
        context = self.service.build_context(self.env.to_dict(), "mars mission")
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "refit.db"), self.env.to_dict(), "refit")
        with tool_context(ToolContext(journal=journal)):
            messages = build_conversation_messages(context)
            self.assertIn("TARGET739", str(messages))
            self.assertEqual(journal.memory_diagnostics["presentation"]["recall_shown"], 0)
            messages.append({"role": "user", "content": "mars mission acceptance?"})
            base = ContextTokenCounter().messages(messages)
            # Leave required policies plus a much smaller working/recall budget.
            overhead = 32768 - context["context_budget"]["fixed_tokens"] - 6000
            tools = [{"type": "function", "function": {"name": "test", "description": "x" * overhead}}]
            with patch.dict(os.environ, {"BINES_CONTEXT_MAX_TOKENS": str(max(32768, base + 1000))}):
                request = bound_request(messages, tools=tools)
        self.assertIn("TARGET739", str(request))
        self.assertGreater(journal.memory_diagnostics["presentation"]["recall_shown"], 0)

    def test_unreachable_soft_target_keeps_prebudgeted_memory_and_discards_raw_history(self):
        self.add_message("mars mission TARGET739", 100)
        for i in range(8):
            self.add_message("context " + "x" * 1000, 101 + i)
        context = self.service.build_context(self.env.to_dict(), "mars mission")
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "preserve.db"), self.env.to_dict(), "preserve")
        with tool_context(ToolContext(journal=journal)):
            frame = build_conversation_messages(context)
            self.assertLessEqual(ContextTokenCounter().messages(frame), 32768)
            self.assertIn("TARGET739", str(frame))
            messages = [*frame, {"role": "user", "content": "UNBOUNDED_RAW " + "x" * 100000},
                        {"role": "user", "content": "current mars question"}]
            tools = [{"type": "function", "function": {"name": "fixture", "description": "x" * 35000}}]
            with patch.dict(os.environ, {"BINES_MODEL_CONTEXT_TOKENS": ""}):
                request = bound_request(messages, tools=tools)
            self.assertEqual(request, [*frame, messages[-1]])
            trace = journal.memory_diagnostics["request"][-1]
            self.assertTrue(trace["bounded_frame_preserved"])
            self.assertTrue(trace["soft_target_exceeded"])
            self.assertFalse(trace["hard_window_exceeded"])
            self.assertEqual(trace["history_removed"], 1)

    def test_explicit_hard_window_can_reduce_a_prebudgeted_frame(self):
        self.add_message("mars mission TARGET739", 100)
        for i in range(20):
            self.add_message("context " + "x" * 1500, 101 + i)
        context = self.service.build_context(self.env.to_dict(), "mars mission")
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "hard-frame.db"), self.env.to_dict(), "hard-frame")
        counter = ContextTokenCounter()
        with tool_context(ToolContext(journal=journal)):
            frame = build_conversation_messages(context)
            tools = [{"type": "function", "function": {"name": "fixture", "description": "x" * 35000}}]
            current = {"role": "user", "content": "mars mission TARGET739"}
            required = context["context_budget"]["fixed_tokens"] + counter.messages([current]) + counter.count({"tools": tools}) + 16
            hard_input_limit = required + 4000
            original = [*frame, current]
            self.assertGreater(counter.messages(original) + counter.count({"tools": tools}) + 16, hard_input_limit)
            with patch.dict(os.environ, {"BINES_MODEL_CONTEXT_TOKENS": str(hard_input_limit + 1024 + 256)}):
                request = bound_request(original, tools=tools, max_output_tokens=1024)
            self.assertLessEqual(counter.messages(request) + counter.count({"tools": tools}) + 16, hard_input_limit)
            self.assertIn("TARGET739", str(request))
            self.assertIn(current, request)
            trace = journal.memory_diagnostics["request"][-1]
            self.assertFalse(trace["bounded_frame_preserved"])
            self.assertFalse(trace["hard_window_exceeded"])

    def test_desktop_skill_rules_keep_the_bounded_frame_discoverable(self):
        from tools import TOOLS_REGISTRY
        from skill_runner import SkillRunner
        self.add_message("mars mission TARGET739", 100)
        context = self.service.build_context(self.env.to_dict(), "mars mission")
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "desktop-frame.db"), self.env.to_dict(), "desktop")
        root = Path(__file__).resolve().parents[2]
        runner = SkillRunner(TOOLS_REGISTRY, skills_dir=root / "skills",
                             enabled_actions_path=root / "server/skill_actions.json")
        with tool_context(ToolContext(journal=journal)), patch.dict(os.environ, {"BINES_MODEL_CONTEXT_TOKENS": ""}):
            frame = build_conversation_messages(context)
            self.assertIn("TARGET739", str(frame))
            model = model_with_responses(response("完成"))
            self.assertEqual(model.run_skill_protocol([*frame, {"role": "user", "content": "mars mission"}],
                                                      runner, source="MANUAL")[0], "完成")
            sent = model.client.chat.completions.create.call_args.kwargs["messages"]
            self.assertIn("TARGET739", str(sent))
            self.assertIn(frame[0], sent)
            self.assertIn(runner.instructions_for_source("MANUAL"), [m["content"] for m in sent])
            self.assertTrue(journal.memory_diagnostics["request"][-1]["bounded_frame_preserved"])

    def test_long_current_qq_input_is_reserved_once_during_request_rebuild(self):
        from common.clock import with_current_time
        self.add_message("needle TARGET739", 100)
        for i in range(29):
            self.add_message("unrelated " + str(i) + " x" * 500, 101 + i)
        query = "needle " + "z" * 22000
        current = "请回复 QQ:fixture 本轮消息：\n" + query
        context = self.service.build_context(self.env.to_dict(), query)
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "long-input.db"), self.env.to_dict(), "long-input")
        tools = [{"type": "function", "function": {"name": "fixture", "description": "t" * 6000}}]
        with patch.dict(os.environ, {"BINES_CONTEXT_MAX_TOKENS": "65536"}), tool_context(ToolContext(journal=journal)):
            prompt = build_conversation_messages(context)
            self.assertGreater(context["context_budget"]["current_input_reserve"], 22000)
            self.assertIn("TARGET739", str(prompt))
            messages = with_current_time([{"role": "system", "content": "r" * 11300}, *prompt,
                                          {"role": "user", "content": current}])
            counter = ContextTokenCounter()
            schema_cost = counter.count({"tools": tools}) + 16
            self.assertGreater(counter.messages(messages) + schema_cost, 65536)
            request = bound_request(messages, tools=tools)
            self.assertEqual(context["context_budget"]["current_input_reserve"], 0)
        self.assertIn("TARGET739", str(request))
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == current for m in request))
        self.assertLessEqual(counter.messages(request) + schema_cost, 65536)

    def test_read_tool_is_registered_for_qq_and_local(self):
        from tools import TOOLS_REGISTRY
        from skill_runner import SkillRunner, QQ_ALLOWED_ACTIONS
        root = Path(__file__).resolve().parents[2]
        runner = SkillRunner(TOOLS_REGISTRY, skills_dir=root / "skills",
                             enabled_actions_path=root / "server/skill_actions.json")
        self.assertIn("read_memory_source", QQ_ALLOWED_ACTIONS)
        self.assertIn("read_memory_source", runner.action_to_skill)

    def test_later_pages_are_confirmed_and_earlier_cited_quotes_retained(self):
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "pages.db"), self.env.to_dict(), "pages")
        item = {"node_id": "source", "node_digest": "digest"}
        for i in range(40):
            span = f"page-{i:02d}-evidence"
            journal.present_memory(item, [span])
            journal.confirm_memory_presentations([{"content": "source " + span}])
            if i == 0:
                journal.cite_memory("source", "answer", span)
        spans = journal.confirmed_memory_presentations["source"]["spans"]
        self.assertIn("page-39-evidence", spans)
        self.assertIn("page-00-evidence", spans)
        self.assertLessEqual(len(spans), 32)

    def test_text_tool_excerpt_keeps_command_pair_and_original_receipt(self):
        journal = MemoryJournal(MemoryOutbox(Path(self.tmp.name) / "receipt.db"), self.env.to_dict(), "receipt")
        result = json.dumps({"data": "x" * 10000, "status": "failed"})
        with tool_context(ToolContext(journal=journal)), patch.dict(os.environ, {"BINES_CONTEXT_MAX_TOKENS": "4096"}):
            journal.execute({"action": "fixture"}, lambda: result)
            command = '[[SKILL_CALL]]{"action":"fixture"}[[/SKILL_CALL]]'
            messages = bound_request([{"role": "user", "content": "current"},
                                      {"role": "assistant", "content": command},
                                      {"role": "system", "content": "<skill_result>\n" + result + "\n</skill_result>"}])
        self.assertIn(command, str(messages))
        self.assertIn(journal.last_tool_receipt, messages[-1]["content"])
        self.assertIn('"status": "failed"', messages[-1]["content"])
        self.assertTrue(messages[-1]["content"].endswith("</skill_result>"))


class RequestBudgetTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"BINES_CONTEXT_MAX_TOKENS": "4096", "BINES_CONTEXT_TOKENIZER_PATH": "",
                                     "BINES_MODEL_CONTEXT_TOKENS": "",
                                     "BINES_WEBSITE_AUTO_OBSERVE": "false"})
        env.start()
        self.addCleanup(env.stop)

    def test_native_tool_rounds_remain_bounded_with_call_pairs(self):
        def call(n):
            data = {"id": str(n), "type": "function", "function": {"name": "search_memory", "arguments": "{}"}}
            return SimpleNamespace(id=str(n), function=SimpleNamespace(name="search_memory", arguments="{}"),
                                   model_dump=lambda **_: data)
        model = model_with_responses(response(tool_calls=[call(1)]), response(tool_calls=[call(2)]), response("done"))
        runner = Mock()
        runner.instructions_for_source.return_value = "tools"
        runner.function_tools.return_value = [{"type": "function", "function": {"name": "search_memory"}}]
        runner.action_to_skill = {"search_memory": "memory-maintenance"}
        runner.execute.side_effect = ["汉" * 50000, "x" * 50000]
        answer, _, _ = model.run_tool_calls([{"role": "user", "content": "CURRENT INPUT"}], runner)
        self.assertEqual(answer, "done")
        self.assertEqual(runner.execute.call_count, 2)
        for request in model.client.chat.completions.create.call_args_list:
            messages, tools = request.kwargs["messages"], request.kwargs["tools"]
            counter = ContextTokenCounter()
            self.assertLessEqual(counter.messages(messages) + counter.count({"tools": tools}) + 16, 4096)
            calls = {c["id"] for m in messages for c in m.get("tool_calls", [])}
            replies = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
            self.assertEqual(calls, replies)
            self.assertIn("CURRENT INPUT", [m.get("content") for m in messages])

    def test_required_input_and_schemas_fail_before_model_call(self):
        model = model_with_responses(response("unused"))
        with patch.dict(os.environ, {"BINES_MODEL_CONTEXT_TOKENS": "5000"}):
            with self.assertRaises(ContextBudgetExceeded) as raised:
                model.complete_text([{"role": "user", "content": "x" * 5000}])
            self.assertEqual(raised.exception.code, "input_budget_exceeded")
            self.assertTrue(raised.exception.diagnostic["hard_window_exceeded"])
            with self.assertRaises(ContextBudgetExceeded):
                bound_request([{"role": "user", "content": "x"}], tools=[{"description": "x" * 5000}])
        model.client.chat.completions.create.assert_not_called()

    def test_output_retry_rechecks_explicit_model_window(self):
        model = model_with_responses(response(finish_reason="length"), response("must not be requested"))
        with patch.dict(os.environ, {"BINES_MODEL_CONTEXT_TOKENS": "5000"}):
            with self.assertRaises(ContextBudgetExceeded):
                model.complete_text([{"role": "user", "content": "x" * 1000}], max_tokens=1000, retry_tokens=4500)
        self.assertEqual(model.client.chat.completions.create.call_count, 1)

    def test_real_static_catalogs_fit_default_qq_and_desktop_requests(self):
        from agents import MainAgent
        from chatBot.messages import Identity
        from tools import TOOLS_REGISTRY
        from skill_runner import SkillRunner, QQ_ALLOWED_ACTIONS
        root = Path(__file__).resolve().parents[2]
        runner = SkillRunner(TOOLS_REGISTRY, skills_dir=root / "skills",
                             enabled_actions_path=root / "server/skill_actions.json",
                             qq_allowed_actions=QQ_ALLOWED_ACTIONS)
        with patch.dict(os.environ):
            os.environ.pop("BINES_CONTEXT_MAX_TOKENS", None)
            for owner in (False, True):
                with tool_context(ToolContext(identity=Identity("fixture", "user", owner=owner), event_id="fixture")):
                    model = model_with_responses(response("done"))
                    self.assertEqual(model.run_tool_calls([{"role": "user", "content": "hello"}], runner)[0], "done")
                    model.client.chat.completions.create.assert_called_once()
            agent = MainAgent.__new__(MainAgent)
            agent.client, agent.model = Mock(), "fake"
            agent.client.chat.completions.create.return_value = response("done")
            agent.set_skill_instructions(runner.instructions_for_source("MANUAL"))
            agent._post_chat_completion({"model": "fake", "messages": agent._ensure_system_prompt([
                {"role": "user", "content": "hello"}]), "max_tokens": 4096})
            agent.client.chat.completions.create.assert_called_once()
            self.assertIn("Arguments:", agent.skill_instructions)

    def test_count_includes_calls_reasoning_and_utf8_is_labelled(self):
        counter = ContextTokenCounter()
        self.assertEqual(counter.method, "utf8_byte_upper_bound")
        self.assertEqual(counter.count("汉"), 3)
        messages = [{"role": "assistant", "content": "", "reasoning_content": "x" * 5000,
                     "tool_calls": [{"id": "one", "function": {"arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "one", "content": "result"}]
        # The original soft target remains compatible; an explicit model
        # window still accounts for reasoning/call metadata before sending.
        self.assertEqual(bound_request(messages), messages)
        with patch.dict(os.environ, {"BINES_MODEL_CONTEXT_TOKENS": "5000"}):
            with self.assertRaises(ContextBudgetExceeded):
                bound_request(messages)

    def test_desktop_client_uses_same_budget(self):
        from agents import MainAgent
        agent = MainAgent.__new__(MainAgent)
        agent.client = Mock()
        agent.client.chat.completions.create.return_value = response("done")
        payload = {"model": "fake", "messages": [
            {"role": "user", "content": "old" * 5000}, {"role": "user", "content": "CURRENT"}]}
        agent._post_chat_completion(payload)
        sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
        self.assertLessEqual(ContextTokenCounter().messages(sent), 4096)
        self.assertIn("CURRENT", [m["content"] for m in sent])

    def test_stream_usage_is_confirmed_from_submitted_messages(self):
        from agents import MainAgent
        from thinking_stream_runner import run_main_agent_rounds
        outbox_dir = TemporaryDirectory(prefix="bines-stream-budget-")
        self.addCleanup(outbox_dir.cleanup)
        journal = MemoryJournal(MemoryOutbox(Path(outbox_dir.name) / "outbox.db"), local_envelope("").to_dict(), "stream")
        journal.present_memory({"node_id": "source-id", "node_digest": "digest"}, ["OLD_EVIDENCE"])
        agent = MainAgent.__new__(MainAgent)
        agent.model, agent.skill_instructions = "fake", ""
        agent.client = Mock()
        agent.client.chat.completions.create.return_value = iter([
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="done"), finish_reason=None)]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=""), finish_reason="stop")]),
        ])
        with tool_context(ToolContext(journal=journal)):
            result = run_main_agent_rounds(messages=[
                {"role": "user", "content": "source-id OLD_EVIDENCE " + "x" * 10000},
                {"role": "user", "content": "current"}], main_agent=agent, skill_runner=Mock(),
                zmq_send=Mock(), is_only_action_or_empty=lambda value: not value.strip(),
                enable_audio=False, source="MANUAL", interrupt_requested=lambda: False, max_skill_rounds=1)
        self.assertFalse(result["error"])
        self.assertFalse(journal.confirmed_memory_presentations)

    def test_excerpt_preserves_failure_outcome_and_durable_receipt(self):
        journal = SimpleNamespace(tool_result_refs={"call": "receipt:original"}, record_memory_diagnostic=Mock())
        token = CURRENT_CONTEXT.set(ToolContext(journal=journal))
        self.addCleanup(CURRENT_CONTEXT.reset, token)
        message = {"role": "tool", "tool_call_id": "call",
                   "content": json.dumps({"data": "x" * 10000, "status": "failed"})}
        result = bound_request([{"role": "assistant", "tool_calls": [{"id": "call"}], "content": ""}, message])
        self.assertIn('"status": "failed"', result[-1]["content"])
        self.assertIn("receipt:original", result[-1]["content"])


if __name__ == "__main__":
    unittest.main()
