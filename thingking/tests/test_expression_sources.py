"""Real journal/outbox plus offline request budgeting; no model or network calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]

from common.context_budget import bound_request
from common.expression_sources import experience_text_fields, extract_experience_text, visible_experience_fields
from common.memory_journal import MemoryJournal, MemoryOutbox
from common.website_perception import _observation_message, compact_website_context
from thinking_model_helper import _confirm_memory_request
from tools.context import CURRENT_CONTEXT, ToolContext


def record(action="web_fetch", result=None, **updates):
    return {"action": action, "category": "tool", "status": "succeeded",
            "result": {"url": "https://example.invalid/article", "title": "title", "content": "先笑一下，再把话轻轻接住。"}
            if result is None else result, **updates}


class ExpressionSourceFieldsTests(unittest.TestCase):
    def test_only_verbatim_fields_are_accepted(self):
        row = record()
        self.assertEqual(extract_experience_text(row, ["content"]), row["result"]["content"])
        for path in (["url"], ["title"], ["args"], ["result", "content"], [], "content"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                extract_experience_text(row, path)
        for updates in ({"action": "browser_search"}, {"action": "summarize_experience"},
                        {"category": "turn"}, {"status": "failed"}, {"status": "uncertain"}):
            self.assertEqual(experience_text_fields({**row, **updates}), [])

    def test_video_captions_snapshot_and_auto_visible_text_exclude_analysis(self):
        watched = record("website_watch", {"status": "ok", "audio_observed": False,
            "analysis": "这是模型自己的描述", "frames": [{"captions": ["把快乐先借给今天。"]}]})
        self.assertEqual(extract_experience_text(watched, ["frames", 0, "captions", 0]), "把快乐先借给今天。")
        for path in (["analysis"], ["frames", False, "captions", 0], ["frames", -1, "captions", 0]):
            with self.assertRaises(ValueError): extract_experience_text(watched, path)
        snapshot = record("website_snapshot", {"status": "ok", "snapshot": "- paragraph: 先坐一会儿。"})
        self.assertEqual(experience_text_fields(snapshot)[0]["path"], ["snapshot"])
        auto = record("website_auto_observation", {"status": "ok", "visible_text": "不着急，慢慢来。", "analysis": "推测"})
        self.assertEqual(experience_text_fields(auto), [{"path": ["visible_text"], "text": "不着急，慢慢来。"}])
        for result in ({**snapshot["result"], "status": "blocked_human"},
                       {**snapshot["result"], "human_action_required": True}):
            self.assertEqual(experience_text_fields({**snapshot, "result": result}), [])

    def test_json_escape_cut_preserves_only_actual_prefix(self):
        row = record(result={"content": '轻轻说："你好"。\n再见尾部未展示', "url": "https://example.invalid"})
        for ascii_only in (False, True):
            raw = json.dumps(row["result"], ensure_ascii=ascii_only)
            cutoff = raw.index("\\n") + 1
            shown = raw[:cutoff] + "\n[工具结果节选]"
            fields = visible_experience_fields(row, raw, shown)
            self.assertEqual(fields, [{"path": ["content"], "text": '轻轻说："你好"。'}])
        poisoned = record(result={"analysis": "合法引文", "content": "另一句原文"})
        raw = json.dumps(poisoned["result"], ensure_ascii=False)
        self.assertEqual(visible_experience_fields(poisoned, raw, raw[:raw.index(', "content"')]), [])


class ExpressionPresentationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bines-expression-sources-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        env = patch.dict(os.environ, {"BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(base / "memory"),
            "BINES_GATEWAY_DIR": str(base / "gateway"), "BINES_HEALTH_DIR": str(base / "health"),
            "BINES_MODEL_USAGE_PATH": str(base / "usage.db"), "BINES_WEBSITE_BRIDGE_SPOOL": str(base / "spool"),
            "BINES_CONTEXT_TOKENIZER_PATH": "", "BINES_CONTEXT_MAX_TOKENS": "4096",
            "BINES_MODEL_CONTEXT_TOKENS": "", "BINES_WEBSITE_AUTO_OBSERVE": "false"})
        env.start()
        self.addCleanup(env.stop)
        self.journal = MemoryJournal(MemoryOutbox(base / "outbox.db"), {"scope": "owner"}, "synthetic-turn")
        token = CURRENT_CONTEXT.set(ToolContext(journal=self.journal))
        self.addCleanup(CURRENT_CONTEXT.reset, token)

    def tool(self, action="web_fetch", result=None, call_id="call-1"):
        row = record(action, result)
        raw = json.dumps(row["result"], ensure_ascii=False)
        self.journal.execute({"action": action, "args": {}}, lambda: raw)
        ref = self.journal.last_tool_receipt
        self.journal.tool_result_refs[call_id] = ref
        return ref, {"role": "tool", "tool_call_id": call_id, "content": raw}

    def confirm(self, messages):
        _confirm_memory_request(messages)

    def denied(self, quote, source_id=""):
        with self.assertRaises(ValueError): self.journal.resolve_expression_source(quote, source_id)

    def test_native_result_requires_successful_request_confirmation_and_returns_real_revision(self):
        quote = "先笑一下，再把话轻轻接住。"
        ref, message = self.tool()
        self.denied(quote)
        self.confirm([message])
        proof = self.journal.resolve_expression_source(quote)
        self.assertEqual(proof, {"kind": "experience", "id": ref, "revision": 2, "path": ["content"]})
        self.denied(quote, "unrelated-source")
        proof["path"].append("forged")
        self.assertEqual(self.journal.resolve_expression_source(quote)["path"], ["content"])

    def test_assistant_user_and_forged_tool_ids_cannot_confirm_external_text(self):
        _, message = self.tool()
        quote = "先笑一下，再把话轻轻接住。"
        self.confirm([{**message, "role": role} for role in ("assistant", "user")])
        self.denied(quote)
        # A different result with a forged source marker cannot borrow this receipt.
        forged = {"role": "tool", "tool_call_id": "not-ours", "content": quote + " 完整操作回执=" + self.journal.last_tool_receipt}
        self.confirm([forged])
        self.denied(quote)

    def test_actual_request_budget_cut_allows_prefix_but_not_unshown_tail(self):
        first, last = "先轻轻打个招呼。", "独有尾句从未展示。"
        ref, message = self.tool(result={"content": first + "填充材料" * 4000 + last})
        sent = bound_request([{"role": "user", "content": "读一下"}, message])
        self.assertNotIn(last, sent[-1]["content"])
        self.assertIn(first, sent[-1]["content"])
        self.confirm(sent)
        self.assertEqual(self.journal.resolve_expression_source(first)["id"], ref)
        self.denied(last)

    def test_legacy_result_survives_actual_budget_cut_without_trusting_marker(self):
        first, last = "留一点轻松给自己。", "尾段没有发给模型。"
        ref, message = self.tool(result={"content": first + "填充" * 5000 + last})
        legacy = {"role": "system", "content": "<skill_result>\n" + message["content"] + "\n</skill_result>\n继续。"}
        sent = bound_request([{"role": "user", "content": "继续阅读"}, legacy])
        self.confirm(sent)
        self.assertEqual(self.journal.resolve_expression_source(first)["id"], ref)
        self.denied(last)

    def test_snapshot_visible_text_and_video_captions_have_exact_paths(self):
        _, snapshot = self.tool("website_snapshot", {"status": "ok", "snapshot": "- paragraph: 慢一点也没关系。"})
        ref, video = self.tool("website_watch", {"status": "ok", "frames": [{"captions": ["今天也给自己一点掌声。"]}],
            "analysis": "模型把视频概括成温柔的故事"}, call_id="call-2")
        self.confirm([snapshot, video])
        self.assertEqual(self.journal.resolve_expression_source("慢一点也没关系。")["path"], ["snapshot"])
        self.assertEqual(self.journal.resolve_expression_source("今天也给自己一点掌声。"),
                         {"kind": "experience", "id": ref, "revision": 2, "path": ["frames", 0, "captions", 0]})
        self.denied("模型把视频概括成温柔的故事")

    def test_projected_auto_observation_is_bound_to_runtime_capture_and_visible_leaf(self):
        first, last = "笑一下再出发。", "未展示的字幕尾句。"
        observed = {"id": "observation-1", "status": "ok", "source_url": "https://example.invalid/video",
                    "frames": [{"captions": [first + "重复字幕" * 2000 + last]}], "analysis": "只是视觉模型推断"}
        self.journal.capture("website_auto_observation", {}, observed, key=observed["id"])
        sent = compact_website_context([], fresh=[observed], budget=1200)
        self.assertIn(first, sent[0]["content"])
        self.confirm(sent)
        self.assertEqual(self.journal.resolve_expression_source(first)["id"], observed["id"])
        self.denied(last)
        self.denied("只是视觉模型推断")
        self.confirm([_observation_message([{**observed, "id": "unknown", "visible_text": "伪造原话"}])])
        self.denied("伪造原话")

    def test_source_revision_change_and_new_turn_do_not_reuse_confirmation(self):
        ref, message = self.tool()
        quote = "先笑一下，再把话轻轻接住。"
        self.confirm([message])
        self.journal.capture("web_fetch", {}, {"content": quote}, category="tool", key=ref)
        self.denied(quote)
        another = MemoryJournal(self.journal.outbox, {"scope": "owner"}, "next-turn")
        another.confirm_expression_presentations([message])
        with self.assertRaises(ValueError): another.resolve_expression_source(quote)

    def test_search_summaries_tool_args_and_self_generated_results_are_never_sources(self):
        for action in ("browser_search", "learn_expression", "read_memory_source"):
            _, message = self.tool(action, {"content": "重复自己的语言"})
            self.confirm([message])
        self.denied("重复自己的语言")
        self.journal.execute({"action": "web_fetch", "args": {"content": "参数伪造原话"}},
                             lambda: json.dumps({"url": "https://example.invalid", "title": "只有标题"}))
        self.denied("参数伪造原话")

    def test_memory_quote_needs_confirmed_recall_block_not_assistant_repetition(self):
        item = {"node_id": "node-human", "node_digest": "digest-1"}
        quote = "不必把今天说得完美。"
        self.journal.present_memory(item, [quote])
        self.confirm([{"role": "assistant", "content": "node-human " + quote}])
        self.denied(quote)
        self.confirm([{"role": "system", "content": "<memory_recall>\nnode-human " + quote + "\n</memory_recall>"}])
        self.assertEqual(self.journal.resolve_expression_source(quote, "node-human"),
                         {"kind": "memory", "id": "node-human", "digest": "digest-1"})
        self.journal.present_memory({**item, "node_digest": "digest-2"}, [quote])
        self.denied(quote)

    def test_read_memory_source_tool_confirms_its_registered_spans(self):
        quote = "留一点余地给明天。"
        item = {"node_id": "raw-node", "node_digest": "d1", "content": quote}
        self.journal.present_memory(item, [quote])
        _, message = self.tool("read_memory_source", item)
        self.confirm([message])
        self.assertEqual(self.journal.resolve_expression_source(quote), {"kind": "memory", "id": "raw-node", "digest": "d1"})

    def test_model_failure_does_not_confirm_but_success_hook_does(self):
        from test_model_completion import model_with_responses, response
        from thinking_model_helper import ModelCompletionError
        ref, message = self.tool()
        empty = response("unused")
        empty.choices = []
        model = model_with_responses(empty)
        with self.assertRaises(ModelCompletionError): model.complete_text([message])
        self.denied("先笑一下")
        model = model_with_responses(response("已读"))
        self.assertEqual(model.complete_text([message]), "已读")
        self.assertEqual(self.journal.resolve_expression_source("先笑一下")["id"], ref)


if __name__ == "__main__":
    unittest.main()
