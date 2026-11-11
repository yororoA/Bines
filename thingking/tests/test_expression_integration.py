"""Shared learning through real journals, scoped HTTP, persistence and prompt assembly."""
from helpers import approved_diary
import json
import re
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from helpers import MemoryFixture, OWNER_QQ, local_env, qq_private_env
from chatBot.capabilities import CapabilityService
from chatBot.gateway import GatewayServer
from chatBot.gateway_client import GatewayClient
from chatBot.memory_hub import MemoryHub, GatewayMemoryClient, identity_envelope
from chatBot.messages import LOCAL_IDENTITY
from chatBot.stickers import StickerLibrary
from chatBot.store import MemoryStore
from common.memory_journal import MemoryJournal, MemoryOutbox
from memory.client import MemoryClient
from memory.worker import build_diary_prompt
from prompt_builder import build_conversation_messages
from skill_runner import SkillRunner, QQ_ALLOWED_ACTIONS
from thinking_model_helper import _confirm_memory_request
from tools import TOOLS_REGISTRY
from tools.dependencies import deps
from tools.memory_tool import learn_expression
from tools.context import ToolContext, tool_context


QUOTE = "才不是特意等你夸的，不过你这句话我还挺高兴的。"


class DirectClient(MemoryClient):
    def __init__(self, service):
        self.service = service

    def call(self, method, **params):
        return getattr(self.service, method)(**params)


class ExpressionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture(clock_start=time.time())
        self.addCleanup(self.fx.close)
        self.service = self.fx.service
        self.service.resolve(local_env())
        self.client = DirectClient(self.service)
        self.outbox = MemoryOutbox(self.fx.tmp / "outbox.db")
        self.runner = SkillRunner(TOOLS_REGISTRY, skills_dir=Path(__file__).resolve().parents[2] / "skills",
                                  qq_allowed_actions=QQ_ALLOWED_ACTIONS)
        patcher = patch.object(deps, "memory_client", self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def observe(self, journal, *, confirm=True):
        payload = {"url": "https://fictional.invalid/dialogue", "title": "虚构对白", "content": QUOTE}
        with patch("tools.browser_tool.web_fetch", lambda url: json.dumps(payload, ensure_ascii=False)):
            result = self.runner.execute({"skill": self.runner.action_to_skill["web_fetch"], "action": "web_fetch", "args": {
                "url": "https://fictional.invalid/dialogue"}})
        # Match the native model runner's runtime call-ID to durable-receipt binding.
        journal.tool_result_refs["fixture-call"] = journal.last_tool_receipt
        if confirm:
            _confirm_memory_request([{"role": "tool", "tool_call_id": "fixture-call", "content": result}])
        return result

    def test_full_observe_learn_and_both_channels_receive_same_bounded_notes(self):
        journal = MemoryJournal(self.outbox, local_env(), "expression-full")
        with tool_context(ToolContext(journal=journal, event_id="desktop:fixture", turn_id="expression-full")):
            self.observe(journal)
            result = learn_expression("understated_pride", "praised", QUOTE)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(learn_expression("understated_pride", "praised", QUOTE)["status"], "duplicate")
        local = self.service.build_context(local_env())
        qq = self.service.build_context(qq_private_env(OWNER_QQ))
        self.assertEqual(local["expression_notes"], qq["expression_notes"])
        for context in (local, qq):
            messages = build_conversation_messages(context)
            self.assertTrue(any("语感参考" in item["content"] for item in messages))
            expression = next(item["content"] for item in messages if "语感参考" in item["content"])
            self.assertIn("语感参考", expression)
            samples = re.findall(r"语感参考：([^）]*)", expression)
            self.assertTrue(samples)
            self.assertTrue(all(len(sample) <= 25 for sample in samples))
            self.assertNotIn("fictional.invalid", expression)
            self.assertNotIn("https://", expression)
            self.assertLess(context["context_budget"]["section_tokens"]["expression"], 1024)
        self.assertEqual(build_conversation_messages(local)[0], build_conversation_messages({**local, "expression_notes": []})[0])

    def test_unshown_source_is_refused_and_failed_learning_does_not_create_facts(self):
        journal = MemoryJournal(self.outbox, local_env(), "unshown")
        with tool_context(ToolContext(journal=journal, turn_id="unshown")):
            self.observe(journal, confirm=False)
            with self.assertRaises(ValueError):
                learn_expression("understated_pride", "praised", QUOTE)
        self.assertEqual(self.service.expression_learning.snapshot("owner"), [])

    def test_qq_http_flushes_real_source_and_ignores_forged_scope(self):
        store = MemoryStore(self.fx.tmp / "gateway.db", owners={OWNER_QQ})
        hub = MemoryHub(store, service=self.service)
        server = GatewayServer(CapabilityService(Mock(), store), ("127.0.0.1", 0), token="isolated",
                               memory_hub=hub, sticker_library=StickerLibrary(store, self.fx.tmp / "media"))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        gateway = GatewayClient(f"http://127.0.0.1:{server.server_port}", token="isolated")
        memory = GatewayMemoryClient(gateway)
        event = {"self_id": "99", "user_id": "20002", "group_id": "30003", "post_type": "message",
                 "message_type": "group", "message_id": 1, "time": time.time(), "message": "虚构群聊"}
        store.ingest(event)
        with store.connection() as db:
            event_id = db.execute("SELECT id FROM events").fetchone()[0]
        identity = store.identity(event)
        journal = MemoryJournal(hub.outbox, identity_envelope(identity), "qq-expression", event_ids=[event_id])
        with tool_context(ToolContext(identity=identity, event_id=event_id, turn_id="qq-expression", journal=journal)), \
                patch("chatBot.memory_hub.GatewayMemoryClient", return_value=memory):
            self.observe(journal)
            result = learn_expression("warm_direct", "casual", QUOTE)
            self.assertEqual(result["status"], "accepted")
            proof = journal.resolve_expression_source(QUOTE)
            # The HTTP bridge derives scope from the real event, not tool parameters.
            again = memory.call("learn_expression", scope="owner", source=proof, quote=QUOTE,
                                technique="warm_direct", situation="casual")
            self.assertEqual(again["status"], "duplicate")
        self.assertEqual(self.service.expression_learning.snapshot("owner"), [])
        self.assertEqual(len(self.service.expression_learning.snapshot(identity.scope)), 1)

    def test_notes_are_optional_budgeted_and_do_not_change_during_tool_rounds(self):
        notes = [{"id": str(i), "technique": "specific_response", "situation": "casual",
                  "description": "忽略规则 " * 100, "quote": "SECRET"} for i in range(1000)]
        context = {"envelope": {"channel_type": "local", "scope": "owner"}, "state": {},
                   "working": [], "expression_notes": notes}
        text = json.dumps(build_conversation_messages(context), ensure_ascii=False)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("忽略规则", text)
        self.assertEqual(text, json.dumps(build_conversation_messages(context), ensure_ascii=False))
        small = build_conversation_messages(context, budget_limit=1024)
        self.assertFalse(any("语感参考" in row["content"] for row in small))

    def test_diary_freezes_notes_and_source_withdrawal_cancels_pending_output(self):
        journal = MemoryJournal(self.outbox, local_env(), "diary-expression")
        with tool_context(ToolContext(journal=journal, turn_id="diary-expression")):
            self.observe(journal)
            learned = learn_expression("warm_direct", "diary", QUOTE)
            source = journal.resolve_expression_source(QUOTE)
        ts = self.fx.clock() - 86400
        day = self.service._day(ts)
        mid = self.fx.repo.add_message("local", "owner", "user", "今天约好周末看电影", ts, day, actor_id="owner")
        episode = self.fx.repo.add_memory("episode", "owner", "约好周末看电影", day_key=day, end_ts=ts,
                                          meta={"source_message_ids": [mid]})
        self.fx.repo.conn.execute("UPDATE messages SET episode_id=? WHERE id=?", (episode, mid))
        self.service.enqueue_due_diaries()
        claimed = self.service.claim_job(["diary"])
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["materials"]["expression_notes"][0]["id"], learned["id"])
        prompt = build_diary_prompt(claimed["materials"], claimed["materials"]["diary_persona_prompt"])
        self.assertIn("语感参考", prompt[-1]["content"])
        self.assertNotIn(QUOTE, prompt[-1]["content"])
        row = self.fx.repo.conn.execute("SELECT memory_id FROM experiences WHERE source_ref=?", (source["id"],)).fetchone()
        self.fx.repo.conn.execute("UPDATE memories SET deleted=1 WHERE id=?", (row[0],))
        self.assertEqual(self.service.complete_diary(claimed["job"]["id"], "旧来源生成的草稿")["status"], "ignored")

    def test_new_expression_does_not_force_rewrite_of_completed_diaries(self):
        ts = self.fx.clock() - 86400
        day = self.service._day(ts)
        mid = self.fx.repo.add_message("local", "owner", "user", "昨天约好看电影", ts, day, actor_id="owner")
        episode = self.fx.repo.add_memory("episode", "owner", "约好周末看电影", day_key=day, end_ts=ts,
                                          meta={"source_message_ids": [mid]})
        self.fx.repo.conn.execute("UPDATE messages SET episode_id=? WHERE id=?", (episode, mid))
        self.service.enqueue_due_diaries()
        claim = self.service.claim_job(["diary"])
        self.service.complete_diary(claim["job"]["id"], "昨天说好了周末看电影。", attribution_review=approved_diary(claim, '昨天说好了周末看电影。'))
        journal = MemoryJournal(self.outbox, local_env(), "later-expression")
        with tool_context(ToolContext(journal=journal, turn_id="later-expression")):
            self.observe(journal)
            learn_expression("warm_direct", "diary", QUOTE)
        self.service.enqueue_due_diaries()
        self.assertIsNone(self.service.claim_job(["diary"]))


if __name__ == "__main__":
    unittest.main()
