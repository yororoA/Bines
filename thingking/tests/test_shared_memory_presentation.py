"""Scoped recall-to-prompt regressions using temporary SQLite and fake indexes."""
from pathlib import Path
import json
import os
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]

from common.context_budget import ContextTokenCounter
from memory.channels import local_envelope, qq_envelope
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from prompt_builder import _allocate_context, _memory_recall_block, _working_messages, build_conversation_messages
from tools.memory_tool import search_memory


class SharedMemoryPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="bines-shared-presentation-")
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": self.tmp.name,
            "BINES_GATEWAY_DIR": self.tmp.name, "BINES_HEALTH_DIR": self.tmp.name,
            "BINES_MODEL_USAGE_PATH": str(Path(self.tmp.name) / "usage.db"),
            "BINES_CONTEXT_MAX_TOKENS": "32768", "BINES_CONTEXT_TOKENIZER_PATH": "",
            "BINES_WEBSITE_AUTO_OBSERVE": "false",
        })
        env.start()
        self.addCleanup(env.stop)
        self.repo = MemoryRepository(":memory:", clock=lambda: 1000.)
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.tmp.name), state_updates=False), clock=lambda: 1000.)
        self.service.sync_identities()

    def raw_turn(self, envelope, texts, *, names=None):
        env = self.service.resolve(envelope)
        self.repo.ensure_conversation(env.conversation_id, env.channel_type, env.scope)
        rows = []
        for i, text in enumerate(texts):
            mid = self.repo.add_message(env.conversation_id, env.scope, "user", text,
                100 + i, "2026-11-09", actor_id=f"qq:{1001 + i}" if env.is_group else "owner",
                actor_name=(names or [""] * len(texts))[i])
            rows.append(self.repo.get_message(mid))
        node = self.service.graph.register_raw(rows)
        return env, rows, node

    def displace_working(self, env):
        for i in range(45):
            self.repo.add_message(env.conversation_id, env.scope, "user", "recent unrelated message " + str(i),
                                  200 + i, "2026-11-09", actor_id=env.actor_id)

    def test_fourth_raw_message_reaches_final_prompt_in_local_and_group_scenes(self):
        for envelope in (local_envelope("").to_dict(), qq_envelope({
                "self_id": "bot", "group_id": "group", "user_id": "1004"}).to_dict()):
            with self.subTest(channel=envelope["channel_type"]):
                env, rows, _ = self.raw_turn(envelope,
                    ["first unrelated", "second unrelated", "third unrelated", "珊瑚计划验收码 CORAL739"])
                self.displace_working(env)
                context = self.service.build_context(env.to_dict(), "珊瑚计划验收码 CORAL739")
                prompt = "\n".join(m["content"] for m in build_conversation_messages(context))
                self.assertIn("CORAL739", prompt)
                hits = [*context["recalled"], *context["recall_backfill"]]
                hit = next(h for h in hits if rows[-1]["id"] in [e.get("source_id") for e in h["evidence"]])
                self.assertEqual(hit["evidence"][0]["source_id"], rows[-1]["id"])
                self.assertLessEqual(len(hit["evidence"]), self.service.settings.evidence_limit)

    def test_vector_matched_fourth_message_survives_without_literal_query_overlap(self):
        env, rows, _ = self.raw_turn(local_envelope("").to_dict(),
            ["one unrelated", "two unrelated", "three unrelated", "纪念物是一条蓝色围巾。"])
        self.displace_working(env)
        self.service.retriever.sync()
        self.service.retriever.index = SimpleNamespace(query_keys=lambda *args: [(f"u:{rows[-1]['id']}:0", .1)])
        context = self.service.build_context(env.to_dict(), "cerulean keepsake")
        prompt = "\n".join(m["content"] for m in build_conversation_messages(context))
        self.assertIn("纪念物是一条蓝色围巾", prompt)

    def test_group_quotes_keep_distinct_speakers_and_do_not_recall_private_scope(self):
        envelope = qq_envelope({"self_id": "bot", "group_id": "group", "user_id": "1001"}).to_dict()
        env, rows, node = self.raw_turn(envelope, ["我的饮品是咖啡", "我的饮品是茶"], names=["同名", "同名"])
        self.repo.add_memory("episode", "owner", "PRIVATE_SECRET 饮品是果汁")
        self.displace_working(env)
        context = self.service.build_context(env.to_dict(), "饮品原话")
        prompt = "\n".join(m["content"] for m in build_conversation_messages(context))
        self.assertIn("user(id=\"qq:1001\",name=\"同名\"): 我的饮品是咖啡", prompt)
        self.assertIn("user(id=\"qq:1002\",name=\"同名\"): 我的饮品是茶", prompt)
        self.assertNotIn("PRIVATE_SECRET", prompt)
        self.assertTrue(all(hit["scope"] == env.scope for hit in context["recalled"]))

    def test_search_tool_keeps_evidence_speaker_metadata_and_scope(self):
        env, rows, node = self.raw_turn(qq_envelope({
            "self_id": "bot", "group_id": "group", "user_id": "1001"}).to_dict(),
            ["我的饮品是咖啡"], names=["张三"])
        hit = self.service.graph.view(node, self.service.retriever.keys, "饮品")
        client = SimpleNamespace(search_with_trace=Mock(return_value={"hits": [hit], "trace": {}}))
        with patch("tools.memory_tool._memory_client", return_value=(client, SimpleNamespace(scope=env.scope))):
            result = search_memory("饮品")
        evidence = result[0]["evidence"][0]
        self.assertEqual((evidence["actor_id"], evidence["actor_name"]), ("qq:1001", "张三"))
        self.assertEqual(client.search_with_trace.call_args.args[1], [env.scope])
        self.assertLessEqual(ContextTokenCounter().count(json.dumps(result, ensure_ascii=False)), 8192)

    def test_cross_channel_budget_selects_newest_then_restores_chronological_order(self):
        context = {"other_channels": [
            {"id": i, "ts": 100 + i, "role": "user", "content": f"UPDATE{i} " + "x" * 600,
             "conversation_id": "qq_private:owner"} for i in range(8)]}
        selected, spent, _ = _allocate_context(context, "local", ContextTokenCounter(), {"other_channels": 1000})
        ids = [m["id"] for m in selected["other_channels"]]
        self.assertIn(7, ids)
        self.assertNotIn(0, ids)
        self.assertEqual(ids, sorted(ids))
        self.assertLessEqual(sum(spent.values()), 1000)

    def test_native_at_positions_preserve_identical_literal_text_and_media(self):
        from chatBot.messages import qq_prompt_body, qq_prompt_view, readable
        chain = [
            {"type": "at", "data": {"qq": "99"}},
            {"type": "text", "data": {"text": "字面例子 [@QQ:99]；"}},
            {"type": "at", "data": {"qq": "all"}},
            {"type": "image", "data": {"file": "https://example.test/a.png"}},
            {"type": "reply", "data": {"id": "7"}},
        ]
        view = qq_prompt_view(chain)
        body, mentions = qq_prompt_body(readable(chain), view["at_spans"])
        self.assertEqual(mentions, ["99", "all"])
        self.assertEqual(body, view["text"])
        self.assertIn("字面例子 [@QQ:99]", body)
        self.assertIn("[image:", body)
        self.assertNotIn("[reply:", body)
        edited = "改过的正文 " + readable(chain)
        self.assertEqual(qq_prompt_body(edited, view["at_spans"]), (edited, []))
        cq = "前文[CQ:at,qq=99]后文&#91;@QQ:99&#93;"
        cq_view = qq_prompt_view(cq)
        self.assertEqual(cq_view["text"], "前文后文[@QQ:99]")
        self.assertEqual(cq_view["mentioned_qq"], ["99"])

    def test_mention_presentation_respects_history_budget_and_marker_boundaries(self):
        from copy import deepcopy
        from chatBot.messages import qq_prompt_view, readable
        counter = ContextTokenCounter()
        chain = [
            {"type": "text", "data": {"text": "前文" * 60}},
            {"type": "at", "data": {"qq": "123456"}},
            {"type": "text", "data": {"text": "后文" * 1000 + "END_OF_FULL_SOURCE"}},
        ]
        item = {"id": 1, "role": "assistant", "ts": 100, "content": readable(chain),
                "qq_at_spans": qq_prompt_view(chain)["at_spans"]}
        context = {"working": [item]}
        original = deepcopy(context)
        for scene in ("qq_private", "qq_group"):
            for quota in (180, 256, 400, 800):
                with self.subTest(scene=scene, quota=quota):
                    selected, spent, _ = _allocate_context(context, scene, counter, {"working": quota})
                    self.assertTrue(selected["working"])
                    self.assertTrue(selected["working"][0]["_partial"])
                    rendered = _working_messages(selected, scene)
                    self.assertLessEqual(counter.messages(rendered), quota)
                    self.assertLessEqual(spent["working"], quota)
                    text = "\n".join(m["content"] for m in rendered)
                    self.assertNotIn("END_OF_FULL_SOURCE", text)
                    self.assertNotIn("[@", text)
                    self.assertIn("[历史节选]", text)
        self.assertEqual(context, original)

    def test_recalled_corrections_and_source_deduplication_survive_presentation(self):
        source = {"source_type": "message", "source_id": 1, "source_ref": "message:1",
                  "role": "user", "actor_id": "qq:1001", "actor_name": "张三", "ts": 100,
                  "content": "计划约在今天"}
        item = {"id": "first", "kind": "message", "content": "计划约在今天", "evidence": [source],
                "corrections": [{"quote": "今天", "replacement": "明天"}]}
        block = _memory_recall_block({"recalled": [item, {**item, "id": "second"}], "recall_query": "计划原话"})
        self.assertEqual(block.count("user(id=\"qq:1001\",name=\"张三\"): 计划约在今天"), 1)
        self.assertIn("当前事实为“明天”", block)

    def test_smaller_raw_evidence_excerpt_keeps_query_match(self):
        content = "无关背景" * 80 + "最终验收码 CORAL739"
        item = {"id": "source", "kind": "message", "content": content, "_display_limit": 60,
                "evidence": [{"source_type": "message", "source_ref": "message:1", "role": "user",
                              "actor_id": "qq:1001", "content": content, "ts": 100}]}
        block = _memory_recall_block({"recalled": [item], "recall_query": "CORAL739"})
        self.assertIn("CORAL739", block)

    def test_raw_evidence_hint_cannot_pull_another_scope_into_view(self):
        env, rows, node = self.raw_turn(qq_envelope({
            "self_id": "bot", "group_id": "group", "user_id": "1001"}).to_dict(), ["本群允许的原话"])
        self.repo.ensure_conversation("local", "local", "owner")
        private = self.repo.add_message("local", "owner", "user", "PRIVATE_SECRET", 100, "2026-11-09",
                                        actor_id="owner", actor_name="主人私密名称")
        hit = self.service.graph.view(node, self.service.retriever.keys, "PRIVATE_SECRET",
            matched_message_id=private, matched_spans=["PRIVATE_SECRET"])
        self.assertEqual([e["source_id"] for e in hit["evidence"]], [rows[0]["id"]])
        self.assertNotIn("PRIVATE_SECRET", json.dumps(hit, ensure_ascii=False))
        self.assertNotIn("主人私密名称", json.dumps(hit, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
