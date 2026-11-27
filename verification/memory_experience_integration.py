"""Real SQLite/HTTP/RPC checks for tool memory and crash recovery; no outgoing QQ."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]
from verification.memory_merge_integration import MemoryMergeIntegration
from verification.napcat_integration import event
from common.memory_journal import MemoryJournal, MemoryOutbox
from chatBot.memory_hub import identity_envelope
from chatBot.messages import LOCAL_IDENTITY
from memory.worker import MemoryWorker
from tools.context import ToolContext, tool_context
from skill_runner import SkillRunner


class ExperienceIntegration(MemoryMergeIntegration):
    def journal(self, ev=None, autonomous=False):
        context = self.context(ev or event("1"))
        journal = MemoryJournal(self.hub.outbox, identity_envelope(context.identity), "test-turn",
                                autonomous=autonomous, candidate={"topic": "天文学"} if autonomous else {},
                                event_ids=[context.event_id])
        return journal, ToolContext(context.identity, context.event_id, context.turn_id, journal=journal)

    def test_full_search_tail_is_recalled_with_url_and_no_human_activity(self):
        journal, ctx = self.journal(autonomous=True)
        result = {"query": "天文学", "content": "普通结果" * 1800 + "\n紫色猎户星云观测站",
                  "url": "https://example.com/orion"}
        self.hub.sync()
        before = self.memory.activity_snapshot()
        with tool_context(ctx):
            journal.capture("browser_search", {"query": "天文学"}, result, category="tool")
        self.hub.sync()
        hits = self.memory.search("紫色猎户星云观测站", ["owner"])
        self.assertTrue(hits)
        evidence = next(ev for hit in hits for ev in hit["evidence"] if ev["action"] == "browser_search")
        self.assertIn("紫色猎户星云观测站", evidence["content"])
        self.assertEqual(evidence["urls"], ["https://example.com/orion"])
        self.assertEqual(evidence["source_type"], "experience")
        self.assertIsNone(evidence["role"])
        self.assertTrue(evidence["autonomous"])
        after = self.memory.activity_snapshot()
        for key in ("messages", "last_user", "last_owner_activity", "state"):
            self.assertEqual(before[key], after[key])
        self.assertGreaterEqual(after["life"]["last_action_at"], before["life"]["last_action_at"])
        self.assertEqual(self.memory.list_memories(kinds=["semantic"])["total"], 0)
        with tool_context(ctx):
            context = self.memory.build_context({}, query="天文学")
        self.assertTrue(any(item["origin"] == "experience" for item in context["recalled"]))

    def test_revision_retry_restart_and_commit_ack_loss(self):
        journal, _ = self.journal()
        entry = journal.begin({"action": "web_fetch", "args": {"url": "https://example.com"}})
        self.hub.sync()
        pending_hit = self.memory.search("example", ["owner"])[0]
        pending_evidence = next(ev for ev in pending_hit["evidence"] if ev["source_type"] == "experience")
        self.assertEqual(pending_evidence["status"], "started")
        journal.finish(entry, "发现朱雀星座", status="succeeded")
        original = self.hub._call
        def lose_ack(method, **params):
            result = original(method, **params)
            if method == "record_experience":
                raise RuntimeError("commit acknowledged too late")
            return result
        with patch.object(self.hub, "_call", side_effect=lose_ack):
            self.hub.sync()
        self.assertEqual(self.hub.outbox.stats()["retrying"], 1)
        restarted = MemoryOutbox(self.store.path)
        restarted.retry(entry[0])
        restarted.sync(original)
        self.assertEqual(restarted.stats()["pending"], 0)
        hits = self.memory.search("朱雀星座", ["owner"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["evidence"][0]["status"], "succeeded")
        self.assertEqual(self.memory.list_memories(kinds=["episode"])["total"], 1)
        # A late delivery of the older "started" version cannot regress the result.
        original("record_experience", envelope=journal.envelope, source_ref=entry[0],
                 record=entry[1], revision=1)
        self.assertEqual(self.memory.search("朱雀星座", ["owner"])[0]["evidence"][0]["status"], "succeeded")

    def test_partial_model_failure_keeps_actual_tool_outcome_and_failed_turn(self):
        from will_runtime import run_intention
        journal, ctx = self.journal(autonomous=True)
        registry = {"web_fetch": lambda url: "已查到龙骨座 https://example.com/car"}
        runner = SkillRunner.__new__(SkillRunner)
        runner.registry, runner.skills = registry, {"web": {"actions": ["web_fetch"]}}
        runner.action_to_skill, runner.qq_allowed_actions = {"web_fetch": "web"}, {"web_fetch"}
        runner.enabled_actions_path = None
        class Model:
            def run_skill_protocol(self, conversation, guarded, **kwargs):
                guarded.execute({"skill": "web", "action": "web_fetch", "args": {"url": "https://example.com/car"}})
                raise RuntimeError("model timed out after fetch")
        with self.assertRaises(RuntimeError), tool_context(ctx):
            run_intention(Model(), runner, {}, {"kind": "curiosity", "intent": "观星", "reason": "兴趣",
                                                "contact_allowed": True}, lambda: "")
        self.hub.sync()
        hits = self.memory.search("龙骨座", ["owner"])
        self.assertEqual(hits[0]["evidence"][0]["status"], "succeeded")
        failures = self.memory.search("model timed out", ["owner"])
        self.assertTrue(any(ev["status"] == "failed" for hit in failures for ev in hit["evidence"]))

    def test_experience_worker_and_diary_keep_provenance_without_facts(self):
        journal, _ = self.journal()
        journal.capture("browser_search", {"query": "仙女座"}, "仙女座可在秋季观测 https://example.com/and")
        self.hub.sync()
        worker = MemoryWorker(self.memory, lambda _: '{"summary":"Bines查到仙女座秋季观测资料。","tags":["Astronomy"]}',
                              lambda: "")
        # Only claim this job to leave the real input's state work independent.
        claimed = self.hub.service.claim_job(kinds=["summarize_experience"])
        worker._experience(claimed["job"], claimed["materials"])
        hit = self.memory.search("仙女座", ["owner"])[0]
        self.assertIn("Bines查到", hit["content"])
        self.assertEqual(hit["evidence"][0]["source_type"], "experience")
        self.assertEqual(self.memory.facts("owner"), [])
        memory = self.hub.service.repo.get_memory(hit["id"])
        self.hub.service.repo.update_memory(memory["id"], day_key="2026-01-01")
        self.hub.service.repo.add_memory("diary", "owner", "秋季观星资料",
            meta={"episode_ids": [memory["id"]], "source_experience_refs": [journal.prefix + ":missing"]})
        diary = self.memory.search("秋季观星资料", ["owner"], kinds=["diary"])[0]
        self.assertEqual(diary["evidence"][0]["source_type"], "experience")

    def test_group_account_private_isolation_and_credential_filter(self):
        journal, _ = self.journal(event("1", group="10"))
        journal.capture("mcp_call", {"api_key": "top-secret", "query": "天鹅座群十资料"},
                        {"password": "never-store", "content": "天鹅座群十资料"})
        self.hub.sync()
        with tool_context(self.context(event("1", group="10", msg_id=12))):
            own = self.memory.search("天鹅座群十资料", ["owner"])
        self.assertTrue(own)
        for ev in (event("1", msg_id=13), event("2"), event("1", group="11"), event("1", group="10", self_id=100)):
            with tool_context(self.context(ev)):
                self.assertEqual(self.memory.search("天鹅座群十资料", ["qq:99:group:10"]), [])
        with self.store.connection() as db:
            raw = "\n".join(row["params"] for row in db.execute("SELECT params FROM memory_outbox"))
        self.assertNotIn("top-secret", raw)
        self.assertNotIn("never-store", raw)

    def test_life_snapshot_scope_cannot_be_forged(self):
        record = {
            "category": "turn", "action": "autonomous_turn", "autonomous": True,
            "turn_id": "owner-life-goal", "candidate": {
                "kind": "initiative", "topic": "管理员私密生命目标", "intent": "只在主人作用域继续",
            },
            "started_at": 1_790_000_000.0, "status": "started", "result": "",
        }
        self.hub.service.record_experience(identity_envelope(LOCAL_IDENTITY),
                                           "owner-life-goal", record, revision=1)
        with tool_context(self.context(event("1", group="10", msg_id=122))):
            group = self.memory.life_snapshot("owner")
        self.assertNotIn("goals", group)
        self.assertNotIn("管理员私密生命目标", json.dumps(group, ensure_ascii=False))
        with tool_context(self.context(event("1", msg_id=123))):
            owner = self.memory.life_snapshot("qq:99:group:10")
        self.assertIn("管理员私密生命目标", json.dumps(owner, ensure_ascii=False))

    def test_life_regulation_is_owner_private_event_bound_and_woken_by_message(self):
        owner_context = self.context(event("1", msg_id=124, text="你可以休息一下"))
        with tool_context(owner_context):
            first = self.memory.regulate_life(
                "qq:forged:group:999", "rest", 30, "连续处理任务后自主休息", "model-forged"
            )
            duplicate = self.memory.regulate_life(
                "owner", "rest", 120, "重试不能延长", "different-model-ref"
            )
        self.assertEqual(first["status"], "updated")
        self.assertEqual(first["life"]["phase"], "resting")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["life"]["regulation"]["until"],
                         first["life"]["regulation"]["until"])
        for ev in (event("2", msg_id=125), event("1", group="10", msg_id=126)):
            with tool_context(self.context(ev)), self.assertRaises(RuntimeError):
                self.memory.regulate_life(
                    "owner", "wake", 30, "不应获得全局生命周期权限", "forged"
                )
        self.context(event("1", msg_id=127, text="现在醒醒"))
        self.hub.sync()
        with tool_context(self.context(event("1", msg_id=128))):
            life = self.memory.life_snapshot("owner")
        self.assertNotEqual(life["phase"], "resting")
        self.assertEqual(life["regulation"], {})

    def test_retrieval_audit_does_not_duplicate_old_memory(self):
        journal, _ = self.journal()
        journal.capture("search_memory", {"query": "麒麟座"}, [{"content": "麒麟座旧资料"}], category="tool")
        self.hub.sync()
        self.assertEqual(self.memory.search("麒麟座旧资料", ["owner"]), [])
        self.assertIsNone(self.hub.service.claim_job(kinds=["summarize_experience"]))

    def test_recalled_source_removes_linked_observation_and_prevents_replay(self):
        journal, ctx = self.journal(event("2", text="请看图片", msg_id=70))
        with self.store.connection() as db:
            message_id = db.execute("SELECT id FROM messages WHERE external_id='99:p:2:70'").fetchone()[0]
        journal.source_message_refs = [self.hub.source + ":" + str(message_id)]
        key = journal.capture("qq_image_analysis", {}, "图片上是赤色凤凰座")
        self.hub.sync()
        self.assertTrue(self.memory.search("赤色凤凰座", [ctx.identity.scope]))
        self.store.ingest(event("2", msg_id=70, post_type="notice", notice_type="friend_recall"))
        self.hub.sync()
        self.assertEqual(self.memory.search("赤色凤凰座", [ctx.identity.scope]), [])
        with self.store.connection() as db:
            db.execute("UPDATE memory_outbox SET synced=0 WHERE source_ref=?", (key,))
        self.hub.sync()
        self.assertEqual(self.memory.search("赤色凤凰座", [ctx.identity.scope]), [])

    def test_recall_before_first_sync_cannot_resurrect_observation(self):
        journal, ctx = self.journal(event("2", text="图片", msg_id=71))
        with self.store.connection() as db:
            message_id = db.execute("SELECT id FROM messages WHERE external_id='99:p:2:71'").fetchone()[0]
        journal.source_message_refs = [self.hub.source + ":" + str(message_id)]
        journal.capture("qq_image_analysis", {}, "青色凤凰座")
        self.store.ingest(event("2", msg_id=71, post_type="notice", notice_type="friend_recall"))
        self.hub.sync()
        self.assertEqual(self.memory.search("青色凤凰座", [ctx.identity.scope]), [])

    def test_gateway_persists_send_before_thinking_receives_receipt(self):
        ctx = self.context(event("1", msg_id=91))
        with tool_context(ctx):
            result = self.gateway.raw_api("send_private_msg", {
                "user_id": "1", "message": "网关先保存这次回复"}, operation_id="send-test")
        self.assertEqual(result["status"], "ok")
        # No QQThinking.remember_send call has run.
        self.hub.sync()
        messages = self.hub.service.repo.recent_scope_messages("owner", "local", 0, 50)
        self.assertTrue(any(m["role"] == "assistant" and "网关先保存" in m["content"] for m in messages))
        with self.store.connection() as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0], 0)

    def test_native_message_read_links_both_trigger_and_referenced_message(self):
        original = event("2", msg_id=95, text="苍蓝星图")
        self.context(original)
        trigger = self.context(event("2", msg_id=96, text="请读取之前的消息"))
        api = self.server.service.client
        api.quoted["95"] = original
        with tool_context(trigger):
            result = self.gateway.raw_api("get_msg", {"message_id": "95"})
        self.assertEqual(result["status"], "ok")
        self.hub.sync()
        rows = self.hub.service.repo.list_memories(scopes=["qq:99:private:2"])
        self.assertTrue(any(len(row["meta"].get("source_message_refs") or []) == 2 for row in rows))
        self.store.ingest(event("2", msg_id=95, post_type="notice", notice_type="friend_recall"))
        self.hub.sync()
        self.assertEqual(self.memory.search("苍蓝星图", ["qq:99:private:2"]), [])

    def test_long_web_page_keeps_text_beyond_model_limit(self):
        from common.deepseek_web import fetch
        journal, ctx = self.journal()
        full = "开头正文" * 7000 + "\n远端完整正文尾部烛龙座"
        with tool_context(ctx), patch("common.deepseek_web._post",
                                      return_value={"title": "星座", "content": full}):
            response = fetch("https://example.com/long")
        self.assertTrue(response["truncated"])
        self.assertNotIn("烛龙座", response["content"])
        self.hub.sync()
        hits = self.memory.search("烛龙座", ["owner"])
        self.assertTrue(hits)
        self.assertIn("烛龙座", hits[0]["evidence"][0]["content"])

    def test_legacy_outcomes_backfill_is_idempotent_and_scoped(self):
        ev = event("1", post_type="bines_proactive")
        ev["proactive"] = {"topic": "白鸟座"}
        self.store.ingest(ev)
        with self.store.connection() as db:
            row = db.execute("SELECT id FROM events WHERE raw=?", (json.dumps(ev, ensure_ascii=False, sort_keys=True),)).fetchone()
        self.store.begin_turn(row["id"])
        self.store.save_turn(row["id"], "done", {"outcomes": [{"action": "browser_search", "result": "白鸟座资料"}],
                                               "reflection": "先留着下次交流"})
        self.assertEqual(self.hub.backfill_experiences()["queued"], 2)
        self.assertEqual(self.hub.backfill_experiences()["queued"], 0)
        self.hub.sync()
        self.assertTrue(self.memory.search("白鸟座资料", ["owner"]))
        self.assertEqual(self.memory.search("白鸟座资料", ["qq:99:group:10"]), [])


if __name__ == "__main__":
    unittest.main()
