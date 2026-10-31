import unittest

from helpers import (
    GROUP_ID,
    OWNER_QQ,
    STRANGER_QQ,
    MemoryFixture,
    local_env,
    qq_group_env,
    qq_private_env,
)
from memory.models import OWNER_SCOPE
from memory.repository import StateConflict


def _summarize_next(fx, summary, facts=()):
    claimed = fx.service.claim_job()
    assert claimed and claimed["job"]["kind"] == "summarize", claimed
    return claimed, fx.service.complete_summary(claimed["job"]["id"], summary, ["Daily"], list(facts), 6)


class ScopeResolutionTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()

    def tearDown(self):
        self.fx.close()

    def test_owner_private_chat_shares_owner_scope(self):
        resolved = self.fx.service.resolve(qq_private_env(OWNER_QQ))
        self.assertEqual(resolved.scope, OWNER_SCOPE)
        self.assertEqual(resolved.actor_id, "owner")
        self.assertTrue(resolved.owner_identity)
        self.assertEqual(resolved.conversation_id, f"qq_private:{OWNER_QQ}")

    def test_stranger_private_chat_is_isolated(self):
        resolved = self.fx.service.resolve(qq_private_env(STRANGER_QQ))
        self.assertEqual(resolved.scope, f"qq_private:{STRANGER_QQ}")
        self.assertFalse(resolved.owner_identity)

    def test_owner_in_group_stays_in_group_scope(self):
        resolved = self.fx.service.resolve(qq_group_env(OWNER_QQ))
        self.assertEqual(resolved.scope, f"qq_group:{GROUP_ID}")
        self.assertTrue(resolved.owner_identity)
        self.assertEqual(resolved.actor_id, f"qq:{OWNER_QQ}")

    def test_client_cannot_forge_scope(self):
        forged = qq_group_env(STRANGER_QQ)
        forged["scope"] = OWNER_SCOPE
        forged["owner_identity"] = True
        resolved = self.fx.service.resolve(forged)
        self.assertEqual(resolved.scope, f"qq_group:{GROUP_ID}")
        self.assertFalse(resolved.owner_identity)

    def test_malformed_qq_event_never_becomes_owner_local(self):
        from memory.channels import envelope_for_turn

        envelope = envelope_for_turn("QQ", {"qq_context": {}}, "hi").to_dict()
        resolved = self.fx.service.resolve(envelope)
        self.assertEqual(resolved.scope, "qq_private:unknown")
        self.assertNotEqual(resolved.conversation_id, "local")

    def test_relinking_owner_moves_existing_conversation(self):
        self.fx.service.record_turn(qq_private_env(STRANGER_QQ), "你好", "嗨")
        self.fx.settings = self.fx.settings.__class__(
            data_dir=self.fx.tmp, owner_qq_ids=frozenset({OWNER_QQ, STRANGER_QQ})
        )
        self.fx.service.settings = self.fx.settings
        self.fx.service.sync_identities()
        context = self.fx.service.build_context(qq_private_env(STRANGER_QQ), "你好")
        self.assertEqual(context["scope"], OWNER_SCOPE)
        self.assertEqual(len(context["working"]), 2)


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()
        service = self.fx.service
        service.repo.add_memory("episode", OWNER_SCOPE, "管理员说他的生日是二月五日，喜欢螃蟹", conversation_id="local",
                                end_ts=self.fx.clock() - 86400)
        service.repo.add_memory("semantic", OWNER_SCOPE, "管理员对花生过敏", subject_id="owner")
        service.repo.add_memory("episode", f"qq_group:{GROUP_ID}", "群里大家讨论了螃蟹料理",
                                conversation_id=f"qq_group:{GROUP_ID}", end_ts=self.fx.clock() - 86400)
        service.repo.add_memory("episode", f"qq_private:{STRANGER_QQ}", "陌生人询问了螃蟹的价格",
                                conversation_id=f"qq_private:{STRANGER_QQ}", end_ts=self.fx.clock() - 86400)
        service.reconcile_index()

    def tearDown(self):
        self.fx.close()

    def _recalled(self, envelope):
        context = self.fx.service.build_context(envelope, "螃蟹")
        visible = {item["content"] for item in context["recalled"]}
        if context["bridge"]:
            visible.add(context["bridge"]["content"])
        return visible, context

    def test_local_and_owner_private_recall_owner_memories(self):
        local_recalled, local_ctx = self._recalled(local_env())
        private_recalled, _ = self._recalled(qq_private_env(OWNER_QQ))
        self.assertIn("管理员说他的生日是二月五日，喜欢螃蟹", local_recalled)
        self.assertEqual(local_recalled, private_recalled)
        self.assertEqual([p["content"] for p in local_ctx["profile"]], ["管理员对花生过敏"])

    def test_group_never_sees_owner_memories(self):
        recalled, context = self._recalled(qq_group_env(OWNER_QQ))
        self.assertEqual(recalled, {"群里大家讨论了螃蟹料理"})
        self.assertEqual(context["profile"], [])

    def test_private_chats_do_not_cross(self):
        recalled, context = self._recalled(qq_private_env(STRANGER_QQ))
        self.assertEqual(recalled, {"陌生人询问了螃蟹的价格"})
        self.assertNotIn("relationship_score", context["state"])

    def test_group_sees_only_public_state(self):
        self.fx.service.put_state(OWNER_SCOPE, {"relationship_score": 133, "npc_state": {"attire": "开衫"}})
        _, group_ctx = self._recalled(qq_group_env(STRANGER_QQ))
        _, owner_ctx = self._recalled(local_env())
        self.assertEqual(group_ctx["state"], {"npc_state": {"attire": "开衫"}})
        self.assertEqual(owner_ctx["state"]["relationship_score"], 133)

    def test_local_conversation_continues_in_owner_private_chat(self):
        self.fx.service.record_turn(local_env(ts=self.fx.clock()), "明天提醒我去取快递", "好的，我记住了")
        _, private_ctx = self._recalled(qq_private_env(OWNER_QQ))
        _, local_ctx = self._recalled(local_env())
        _, group_ctx = self._recalled(qq_group_env(OWNER_QQ))
        self.assertEqual(private_ctx["working"], [])
        self.assertEqual([m["content"] for m in private_ctx["other_channels"]], ["明天提醒我去取快递", "好的，我记住了"])
        self.assertEqual(len(local_ctx["working"]), 2)
        self.assertEqual(local_ctx["other_channels"], [])
        self.assertEqual(group_ctx["other_channels"], [])


class ConsolidationTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()

    def tearDown(self):
        self.fx.close()

    def _talk(self, rounds, envelope_factory=local_env):
        for i in range(rounds):
            self.fx.clock.advance(60)
            self.fx.service.record_turn(envelope_factory(ts=self.fx.clock()), f"第{i}句用户消息", f"第{i}句回复")

    def test_summary_job_is_idempotent(self):
        self._talk(3)
        claimed, result = _summarize_next(self.fx, "管理员和助手聊了日常", [{"subject": "owner", "content": "管理员喜欢冰块"}])
        self.assertEqual(result["status"], "done")
        again = self.fx.service.complete_summary(claimed["job"]["id"], "重复提交", [], [], 5)
        self.assertEqual(again["status"], "ignored")
        episodes = self.fx.repo.list_memories(kinds=["episode"])
        self.assertEqual([e["content"] for e in episodes], ["管理员和助手聊了日常"])
        facts = self.fx.repo.list_memories(kinds=["semantic"])
        self.assertEqual([(f["content"], f["subject_id"]) for f in facts], [("管理员喜欢冰块", "owner")])
        self.assertEqual(self.fx.repo.pending_message_count("local"), 2)

    def test_failed_job_is_retried_then_parked(self):
        self._talk(3)
        for attempt in range(self.fx.settings.job_max_attempts):
            claimed = self.fx.service.claim_job()
            self.assertIsNotNone(claimed, f"attempt {attempt}")
            status = self.fx.service.fail_job(claimed["job"]["id"], "model error")["status"]
            self.fx.clock.advance(10 ** 6)
        self.assertEqual(status, "failed")
        self.assertEqual(self.fx.repo.pending_message_count("local"), 6)

    def test_expired_lease_is_reclaimed(self):
        self._talk(3)
        first = self.fx.service.claim_job()
        self.assertIsNone(self.fx.service.claim_job())
        self.fx.repo.conn.execute("UPDATE jobs SET lease_until = 0 WHERE id = ?", (first["job"]["id"],))
        second = self.fx.service.claim_job()
        self.assertEqual(second["job"]["id"], first["job"]["id"])

    def test_duplicate_fact_is_not_stored_twice(self):
        self._talk(3)
        _summarize_next(self.fx, "第一段", [{"subject": "owner", "content": "管理员喜欢冰块"}])
        self._talk(3)
        _summarize_next(self.fx, "第二段", [{"subject": "owner", "content": "管理员喜欢冰块"}])
        self.assertEqual(self.fx.repo.count_memories(kinds=["semantic"]), 1)

    def test_bridge_episode_survives_window(self):
        self._talk(3)
        _summarize_next(self.fx, "较早的对话摘要")
        self._talk(3)
        context = self.fx.service.build_context(local_env(), "随便聊聊")
        self.assertEqual(context["bridge"]["content"], "较早的对话摘要")
        self.assertNotIn("较早的对话摘要", [r["content"] for r in context["recalled"]])

    def test_group_fact_subject_must_be_participant(self):
        for i in range(3):
            self.fx.clock.advance(60)
            env = qq_group_env(STRANGER_QQ, ts=self.fx.clock(), name="小明")
            self.fx.service.record_turn(env, f"群消息{i}", f"回复{i}")
        _summarize_next(self.fx, "群聊摘要", [
            {"subject": f"qq:{STRANGER_QQ}", "content": "小明喜欢打篮球"},
            {"subject": "owner", "content": "伪造的管理员事实"},
        ])
        facts = {f["content"]: f for f in self.fx.repo.list_memories(kinds=["semantic"])}
        self.assertEqual(facts["小明喜欢打篮球"]["subject_id"], f"qq:{STRANGER_QQ}")
        self.assertEqual(facts["小明喜欢打篮球"]["scope"], f"qq_group:{GROUP_ID}")
        self.assertNotIn("伪造的管理员事实", facts)
        context = self.fx.service.build_context(qq_group_env(STRANGER_QQ), "篮球")
        self.assertIn("小明喜欢打篮球", [p["content"] for p in context["profile"]])

    def test_diary_job_for_live_episodes(self):
        from dataclasses import replace
        self.fx.service.settings = replace(self.fx.settings, summarize_chunk=6)
        self._talk(3)
        _summarize_next(self.fx, "今天一起听了歌")
        self.fx.clock.advance(2 * 86400)
        claimed = self.fx.service.claim_job()
        self.assertEqual(claimed["job"]["kind"], "diary")
        self.assertEqual(claimed["materials"]["episodes"], ["今天一起听了歌"])
        self.fx.service.complete_diary(claimed["job"]["id"], "今天和管理员一起听歌，很开心。")
        self.assertIsNone(self.fx.service.claim_job())
        self.assertEqual(self.fx.repo.count_memories(kinds=["diary"]), 1)

    def test_system_events_are_not_stored_as_user_messages(self):
        self.fx.service.record_turn(local_env(), "[System Event: User Online] (...)", "欢迎回来")
        working = self.fx.service.build_context(local_env(), "")["working"]
        self.assertEqual([m["role"] for m in working], ["assistant"])


class RuntimeMemoryBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture(summarize_trigger=40, summarize_chunk=30)
        self.service, self.repo = self.fx.service, self.fx.repo
        self.service.resolve(local_env())

    def tearDown(self):
        self.fx.close()

    def _message(self, text="a real event", ts=None, **fields):
        ts = self.fx.clock() if ts is None else ts
        return self.repo.add_message(
            "local", OWNER_SCOPE, fields.pop("role", "user"), text, ts, self.service._day(ts),
            actor_id="owner", **fields,
        )

    def _episode(self, text="past event", ts=None, **fields):
        ts = self.fx.clock() - 86400 if ts is None else ts
        return self.repo.add_memory(
            "episode", OWNER_SCOPE, text, day_key=self.service._day(ts),
            start_ts=ts, end_ts=ts, **fields,
        )

    def test_pending_filters_ignore_archived_recalled_and_system_messages(self):
        valid = self._message(ts=self.fx.clock() - 10000)
        excluded = [
            self._message(origin="archived"),
            self._message(origin="recalled"),
            self._message("[消息已撤回]"),
            self._message("[MESSAGE RECALLED]"),
            self._message(" [System Event: User Online] "),
            self._message("[系统：自动操作]"),
            self._message("[Screen Monitor]"),
            self._message(role="system"),
            self._message("   "),
        ]
        self.assertEqual(self.repo.pending_message_count("local"), 1)
        self.assertEqual([m["id"] for m in self.repo.oldest_pending_messages("local", 30)], [valid])
        rows = self.repo.idle_pending_conversations(self.fx.clock() - 60, 1)
        self.assertEqual([(r["n"], r["last_ts"]) for r in rows], [(1, self.fx.clock() - 10000)])
        self.assertEqual(self.repo.idle_pending_conversations(self.fx.clock(), 2), [])
        self.assertIsNotNone(self.repo.get_message(excluded[0]))
        self.assertNotIn(excluded[0], [m["id"] for m in self.repo.recent_messages("local", 100)])
        self.assertNotIn(excluded[0], [
            m["id"] for m in self.repo.recent_scope_messages(OWNER_SCOPE, "another", 0, 100)
        ])

    def test_legacy_cross_day_summary_uses_first_day_and_exact_ids(self):
        today = self._message("today", ts=self.fx.clock())
        later = self._message("yesterday later", ts=self.fx.clock() - 86400 + 10)
        archived = self._message("old raw", ts=self.fx.clock() - 86400, origin="archived")
        earlier = self._message("yesterday earlier", ts=self.fx.clock() - 86400)
        same_time = self._message("same timestamp", ts=self.fx.clock() - 86400)
        expected = [earlier, same_time, later]
        self.assertEqual([m["id"] for m in self.repo.oldest_pending_messages("local", 30)], expected)
        job_id = self.repo.enqueue_job("summarize", OWNER_SCOPE, {"from_id": today, "to_id": same_time},
                                       "old-cross-day", conversation_id="local")
        claimed = self.service.claim_job(kinds=["summarize"])
        self.assertEqual(claimed["job"]["id"], job_id)
        self.assertEqual([m["id"] for m in claimed["materials"]["messages"]], expected)
        self.assertEqual(self.repo.get_job(job_id)["payload"]["source_message_ids"], expected)
        result = self.service.complete_summary(job_id, "only yesterday")
        episode = self.repo.get_memory(result["episode_id"])
        self.assertEqual(episode["day_key"], self.service._day(self.fx.clock() - 86400))
        self.assertEqual(episode["meta"]["source_message_ids"], expected)
        self.assertNotIn("message_range", episode["meta"])
        self.assertTrue(all(self.repo.get_message(i)["episode_id"] == episode["id"] for i in expected))
        self.assertIsNone(self.repo.get_message(today)["episode_id"])
        self.assertIsNone(self.repo.get_message(archived)["episode_id"])

    def test_explicit_summary_ids_do_not_consolidate_interleaved_messages(self):
        first = self._message("first")
        middle = self._message("not in job")
        last = self._message("last")
        self.repo.enqueue_job(
            "summarize", OWNER_SCOPE,
            {"from_id": first, "to_id": last, "source_message_ids": [first, last]},
            "explicit-ids", conversation_id="local",
        )
        claimed = self.service.claim_job(kinds=["summarize"])
        self.assertEqual(claimed["materials"]["source_message_ids"], [first, last])
        result = self.service.complete_summary(claimed["job"]["id"], "selected events")
        self.assertEqual(self.repo.get_memory(result["episode_id"])["meta"]["source_message_ids"], [first, last])
        self.assertIsNone(self.repo.get_message(middle)["episode_id"])
        self.assertEqual(self.repo.mark_consolidated_ids("another", [middle], "wrong"), 0)

    def test_small_past_tail_blocks_diary_and_is_summarized_even_with_live_today(self):
        episode = self._episode()
        past = self._message("late past detail", ts=self.fx.clock() - 86400)
        today = self._message("today is active")
        self.assertEqual(self.service.enqueue_due_diaries(), 0)
        self.assertIsNone(self.service.claim_job(kinds=["diary"]))
        summary = self.service.claim_job(kinds=["summarize"])
        self.assertEqual(summary["materials"]["source_message_ids"], [past])
        result = self.service.complete_summary(summary["job"]["id"], "late past detail")
        diary = self.service.claim_job(kinds=["diary"])
        self.assertEqual(set(diary["job"]["payload"]["episode_ids"]), {episode, result["episode_id"]})
        self.assertIsNone(self.repo.get_message(today)["episode_id"])

    def test_diary_only_fills_valid_past_days_with_active_evidence(self):
        for day in ("1900-01-01", self.service._day(self.fx.clock()), "2999-01-01", "2026-2-01"):
            with self.subTest(day=day):
                self.repo.enqueue_job("diary", OWNER_SCOPE, {"day_key": day}, "empty:" + day)
        deleted = self._episode("deleted original", ts=self.fx.clock() - 2 * 86400)
        self.repo.delete_memory(deleted)
        self._episode("[System Event: User Online]", ts=self.fx.clock() - 3 * 86400)
        self.assertIsNone(self.service.claim_job(kinds=["diary"]))
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 0)
        legacy = self._episode("legacy evidence", origin="legacy")
        self.assertEqual(self.service.enqueue_due_diaries(), 1)
        job = self.service.claim_job(kinds=["diary"])
        self.assertEqual(job["materials"]["episode_ids"], [legacy])
        self.assertEqual(job["materials"]["state"], {})
        self.assertEqual(self.service.complete_diary(job["job"]["id"], "legacy diary")["status"], "done")
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 1)

    def test_deleted_diary_can_be_regenerated_from_same_episode_set(self):
        self._episode()
        first = self.service.claim_job(kinds=["diary"])
        diary = self.service.complete_diary(first["job"]["id"], "first diary")["diary_id"]
        self.service.delete_memory(diary)
        self.assertEqual(self.service.enqueue_due_diaries(), 1)
        second = self.service.claim_job(kinds=["diary"])
        self.assertEqual(second["materials"]["episodes_hash"], first["materials"]["episodes_hash"])
        replacement = self.service.complete_diary(second["job"]["id"], "replacement diary")["diary_id"]
        self.assertNotEqual(replacement, diary)
        self.assertTrue(self.repo.get_memory(diary)["deleted"])
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 1)

    def test_late_episode_and_edited_episode_overwrite_one_active_diary(self):
        first = self._episode("first event", meta={"source_message_ids": [11], "source_memory_ids": ["old-1"]})
        day = self.repo.get_memory(first)["day_key"]
        deleted = self.repo.add_memory("diary", OWNER_SCOPE, "deleted diary", day_key=day)
        self.repo.delete_memory(deleted)
        job = self.service.claim_job(kinds=["diary"])
        diary_id = self.service.complete_diary(job["job"]["id"], "first diary")["diary_id"]
        self.assertNotEqual(diary_id, deleted)
        second = self._episode("late event", meta={"source_message_ids": [22], "source_memory_ids": ["old-2"]})
        self.assertEqual(self.service.enqueue_due_diaries(), 1)
        job = self.service.claim_job(kinds=["diary"])
        self.assertEqual(set(job["materials"]["episode_ids"]), {first, second})
        result = self.service.complete_diary(job["job"]["id"], "revised diary")
        self.assertEqual(result["diary_id"], diary_id)
        diary = self.repo.get_memory(diary_id)
        self.assertEqual(diary["content"], "revised diary")
        self.assertEqual(set(diary["meta"]["source_message_ids"]), {11, 22})
        self.assertEqual(set(diary["meta"]["source_memory_ids"]), {"old-1", "old-2"})
        self.assertEqual(self.repo.count_memories(kinds=["diary"], day_key=day), 1)
        self.assertTrue(self.repo.get_memory(deleted)["deleted"])
        self.assertEqual(self.service.enqueue_due_diaries(), 0)
        digest = diary["meta"]["episodes_hash"]
        self.repo.update_memory(second, content="corrected late event")
        self.assertEqual(self.service.enqueue_due_diaries(), 1)
        job = self.service.claim_job(kinds=["diary"])
        self.assertNotEqual(job["job"]["payload"]["episodes_hash"], digest)
        self.assertEqual(self.service.complete_diary(job["job"]["id"], "corrected diary")["diary_id"], diary_id)

    def test_changed_evidence_during_generation_rejects_stale_diary(self):
        first = self._episode()
        job = self.service.claim_job(kinds=["diary"])
        second = self._episode("late arrival")
        self.assertEqual(self.service.enqueue_due_diaries(), 0)
        result = self.service.complete_diary(job["job"]["id"], "stale output")
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 0)
        replacement = self.service.claim_job(kinds=["diary"])
        self.assertEqual(set(replacement["materials"]["episode_ids"]), {first, second})
        self.service.complete_diary(replacement["job"]["id"], "complete output")
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 1)

    def test_pending_message_during_generation_rejects_diary(self):
        self._episode()
        job = self.service.claim_job(kinds=["diary"])
        self._message("unfinished", ts=self.fx.clock() - 86400)
        self.assertEqual(self.service.complete_diary(job["job"]["id"], "too early")["status"], "ignored")
        self.assertEqual(self.repo.count_memories(kinds=["diary"]), 0)
        self.assertIsNone(self.service.claim_job(kinds=["diary"]))
        self.assertIsNotNone(self.service.claim_job(kinds=["summarize"]))

    def test_diary_uses_historical_state_before_shifted_day_boundary(self):
        from datetime import datetime
        from memory.models import ZONE
        self.fx.clock.now = datetime(2026, 10, 20, 12, tzinfo=ZONE).timestamp()
        self.service.put_state(OWNER_SCOPE, {"location": "yesterday"})
        episode = self._episode(ts=self.fx.clock())
        self.fx.clock.now = datetime(2026, 10, 21, 3, 59, tzinfo=ZONE).timestamp()
        self.service.put_state(OWNER_SCOPE, {"location": "before day rollover"})
        self.fx.clock.now = datetime(2026, 10, 21, 4, tzinfo=ZONE).timestamp()
        self.service.put_state(OWNER_SCOPE, {"location": "today"})
        job = self.service.claim_job(kinds=["diary"])
        self.assertEqual(job["materials"]["day_key"], self.repo.get_memory(episode)["day_key"])
        self.assertEqual(job["materials"]["state"], {"location": "before day rollover"})
        self.assertEqual(self.service.get_state(OWNER_SCOPE)["data"], {"location": "today"})

    def test_diary_never_falls_back_to_current_or_older_day_state(self):
        self.fx.clock.advance(-2 * 86400)
        self.service.put_state(OWNER_SCOPE, {"location": "two days ago"})
        self.fx.clock.advance(2 * 86400)
        self._episode()
        self.service.put_state(OWNER_SCOPE, {"location": "today"})
        job = self.service.claim_job(kinds=["diary"])
        self.assertEqual(job["materials"]["state"], {})

    def test_backdated_version_inheriting_future_state_is_not_used(self):
        past = self.fx.clock() - 86400
        self.repo.put_state(OWNER_SCOPE, {"future": "today"})
        self.repo.put_state(OWNER_SCOPE, {"future": "inherited"}, effective_at=past)
        self._episode()
        job = self.service.claim_job(kinds=["diary"])
        self.assertEqual(job["materials"]["state"], {})

    def test_repair_diary_with_matching_episode_is_not_regenerated(self):
        episode = self._episode(origin="repair", meta={"source_message_ids": [1]})
        day = self.repo.get_memory(episode)["day_key"]
        self.repo.add_memory("diary", OWNER_SCOPE, "repaired diary", day_key=day,
                             origin="repair", meta={"episode_id": episode, "source_message_ids": [1]})
        self.assertEqual(self.service.enqueue_due_diaries(), 0)
        self._episode("late real event")
        self.assertEqual(self.service.enqueue_due_diaries(), 1)

    def test_archived_source_remains_evidence_and_can_be_recalled(self):
        message = self._message("a blue scarf", origin="archived", source_ref="archived-source")
        unrelated = self._message("another blue scarf detail", origin="archived")
        episode = self._episode("a blue scarf was given", meta={"source_message_ids": [message]},
                                 conversation_id="local")
        other_episode = self._episode("unrelated event", meta={"source_message_ids": [unrelated]})
        day = self.repo.get_memory(episode)["day_key"]
        diary = self.repo.add_memory("diary", OWNER_SCOPE, "scarf diary", day_key=day,
                                     meta={"episode_ids": [episode], "source_message_ids": [message]})
        quote = self.repo.add_memory("excerpt", OWNER_SCOPE, "scarf quotation",
                                     meta={"curated_quote": True, "source_message_ids": [message]})
        self.service.reconcile_index()
        hits = self.service.search("blue scarf", [OWNER_SCOPE], kinds=["episode"])
        evidence = [e for h in hits if h["id"] == episode for e in h.get("evidence") or []]
        self.assertTrue(any(e["source_id"] == message and "blue scarf" in e["content"] for e in evidence))
        self.service.record_message(local_env(), "user", "[消息已撤回]",
                                    source_ref="archived-source", recalled=True)
        self.assertTrue(all(self.repo.get_memory(i)["deleted"] for i in (episode, diary, quote)))
        self.assertFalse(self.repo.get_memory(other_episode)["deleted"])
        self.assertEqual(self.repo.get_message(message)["content"], "[消息已撤回]")
        self.assertEqual(self.repo.get_message(message)["origin"], "recalled")
        self.assertEqual(self.repo.pending_message_count("local"), 0)

    def test_default_recall_keeps_curated_quotes_not_raw_messages(self):
        raw = self._message("我新买了一条蓝色围巾")
        excerpt = self.repo.add_memory("excerpt", OWNER_SCOPE, "蓝色围巾的原始转录")
        quote = self.repo.add_memory("excerpt", OWNER_SCOPE, "蓝色围巾的精选原话",
                                     meta={"curated_quote": 1, "source_message_ids": [raw]})
        context = self.service.build_context(local_env(), "蓝色围巾")
        self.assertEqual(context["recall_query"], "蓝色围巾")
        self.assertNotIn(excerpt, [m["id"] for m in context["recalled"]])
        self.assertFalse(any(m["kind"] == "message" for m in context["recalled"]))
        self.assertTrue(any(m["id"] == quote and m["meta"]["curated_quote"] == 1 for m in context["recalled"]))
        explicit = self.service.search("蓝色围巾", [OWNER_SCOPE], kinds=["message"])
        self.assertTrue(any(h["message_id"] == raw for h in explicit))

    def test_profile_is_bounded_and_keeps_legacy_identity(self):
        fixed = self.repo.add_memory("semantic", OWNER_SCOPE, "fixed identity",
                                     subject_id="owner", origin="legacy", importance=1)
        target = None
        for i in range(40):
            target = self.repo.add_memory("semantic", OWNER_SCOPE, f"stable detail {i}",
                                          subject_id="owner", importance=9)
        context = self.service.build_context(local_env(), "stable detail 39")
        self.assertLessEqual(len(context["profile"]), max(self.fx.settings.profile_k, 12))
        self.assertIn(fixed, [f["id"] for f in context["profile"]])
        hits = self.service.search("stable detail 39", [OWNER_SCOPE], kinds=["semantic"])
        self.assertTrue(any(h["id"] == target for h in hits))

    def test_memory_timezone_is_independent_of_process_timezone(self):
        import os
        import subprocess
        from datetime import datetime, timezone
        ts = datetime(2026, 10, 20, 20, 30, tzinfo=timezone.utc).timestamp()
        code = (
            "from memory.models import day_key; from memory.worker import _format_ts; "
            f"print(day_key({ts})); print(_format_ts({ts}))"
        )
        import sys
        for process_zone in ("UTC", "America/New_York"):
            with self.subTest(process_zone=process_zone):
                result = subprocess.run(
                    [sys.executable, "-c", code], capture_output=True, text=True, check=True,
                    env={**os.environ, "TZ": process_zone, "BINES_MEMORY_TIMEZONE": "Asia/Shanghai",
                         "PYTHONPATH": os.pathsep.join(sys.path), "PYTHONDONTWRITEBYTECODE": "1"},
                )
                self.assertEqual(result.stdout.splitlines(), ["2026-10-21", "10/21 04:30"])
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True,
            env={**os.environ, "TZ": "Asia/Shanghai", "BINES_MEMORY_TIMEZONE": "UTC",
                 "PYTHONPATH": os.pathsep.join(sys.path), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.stdout.splitlines(), ["2026-10-20", "10/20 20:30"])

    def test_generation_prompts_constrain_date_facts_and_no_transcript_replay(self):
        from memory.worker import build_diary_prompt, build_summary_prompt
        prompt = build_diary_prompt({"day_key": "2026-10-20", "episodes": ["当日确实听了歌"], "state": {}},
                                    "日记人格")
        self.assertEqual(prompt[0]["content"], "日记人格")
        self.assertIn("日期：2026-10-20", prompt[1]["content"])
        self.assertIn("当日确实听了歌", prompt[1]["content"])
        self.assertIn("排除系统事件、模型自我推测", prompt[1]["content"])
        self.assertNotIn("当前状态", prompt[1]["content"])
        summary = build_summary_prompt({
            "scope": OWNER_SCOPE,
            "messages": [{"id": 1, "role": "user", "actor_id": "owner", "content": "heard music",
                          "ts": self.fx.clock()}],
        })
        self.assertIn("事实与事件", summary[0]["content"])
        self.assertIn("不逐句复述 User/Assistant", summary[1]["content"])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()

    def tearDown(self):
        self.fx.close()

    def test_optimistic_concurrency(self):
        first = self.fx.service.put_state(OWNER_SCOPE, {"relationship_score": 1})
        self.fx.service.put_state(OWNER_SCOPE, {"relationship_score": 2}, expected_version=first["version"])
        with self.assertRaises(StateConflict):
            self.fx.service.put_state(OWNER_SCOPE, {"relationship_score": 3}, expected_version=first["version"])
        self.assertEqual(self.fx.service.get_state(OWNER_SCOPE)["data"], {"relationship_score": 2})

    def test_new_message_updates_state_without_summary_and_is_idempotent(self):
        from dataclasses import replace
        from memory.worker import MemoryWorker
        from memory.server import dispatch
        self.fx.service.settings = replace(self.fx.settings, state_updates=True)
        service = self.fx.service
        service.put_state("owner", {"relationship_score": 133})
        service.record_turn(local_env(), "送你一副夜莺耳机 谢谢你一直陪我", "我收下啦")
        job = service.claim_job(kinds=["update_state"])
        self.assertEqual(job["materials"]["state"]["relationship_score"], 133)
        class Client:
            def claim_job(inner, kinds=None):
                return job
            def complete_state_update(inner, job_id, changes, expected_version=None, **claim):
                return service.complete_state_update(job_id, changes, expected_version, **claim)
            def fail_job(inner, job_id, error, **claim):
                raise AssertionError(error)
        worker = MemoryWorker(Client(), lambda m: "", lambda: "",
                              state_complete_text=lambda m: '{"relationship_delta":1,"add_item":"夜莺耳机"}')
        worker.run_once()
        self.assertEqual(service.get_state("owner")["data"]["relationship_score"], 134)
        self.assertEqual(service.get_state("owner")["data"]["inventory"], ["夜莺耳机"])
        self.assertEqual(dispatch(service, {"method": "complete_state_update",
            "params": {"job_id": job["job"]["id"], "changes": {"relationship_delta": 1}}})["result"]["status"], "ignored")
        self.assertEqual(service.get_state("owner")["data"]["relationship_score"], 134)

    def test_group_background_state_is_scoped_to_speaker(self):
        from dataclasses import replace
        self.fx.service.settings = replace(self.fx.settings, state_updates=True)
        service = self.fx.service
        service.put_state("owner", {"relationship_score": 133})
        service.record_turn(qq_group_env(OWNER_QQ), "群里约定", "")
        job = service.claim_job(kinds=["update_state"])
        self.assertEqual(job["job"]["scope"], f"qq_group:{GROUP_ID}:user:{OWNER_QQ}")
        service.complete_state_update(job["job"]["id"], {"active_quest": "群里约定", "relationship_delta": 1})
        self.assertEqual(service.get_state("owner")["data"]["relationship_score"], 133)
        self.assertNotIn("active_quest", service.build_context(qq_group_env(STRANGER_QQ), "")["state"])
        self.assertEqual(service.build_context(qq_group_env(OWNER_QQ), "")["state"]["active_quest"], "群里约定")

    def test_legacy_profile_and_score_are_visible_without_vector_results(self):
        import prompt_builder
        from unittest.mock import patch
        for i in range(12):
            self.fx.repo.add_memory("semantic", "owner", f"关于管理员：稳定事实{i}", subject_id="owner")
        self.fx.service.put_state("owner", {"relationship_score": 0})
        with patch.object(self.fx.index, "query", return_value=[]):
            context = self.fx.service.build_context(local_env(), "稳定事实11")
        text = str(prompt_builder.build_conversation_messages(context))
        self.assertIn("稳定事实11", text)
        self.assertIn("好感度数值（范围 -200 到 200）：0", text)

    def test_old_relevant_memory_beats_new_unrelated_fragment(self):
        old = {"kind": "episode", "end_ts": self.fx.clock() - 300 * 86400, "importance": 8}
        recent = {"kind": "episode", "end_ts": self.fx.clock(), "importance": 5}
        self.assertGreater(self.fx.service._score(old, 0.1, self.fx.clock()),
                           self.fx.service._score(recent, 0.8, self.fx.clock()))

    def test_keyword_search_finds_name_inside_long_legacy_transcript(self):
        from unittest.mock import patch
        target = self.fx.repo.add_memory("episode", "owner", "闲聊。" * 1000 + "竹林鸟夜莺耳机", importance=5)
        self.fx.repo.add_memory("episode", "owner", "其他聊天", importance=100)
        with patch.object(self.fx.index, "query", return_value=[]):
            hits = self.fx.service.search("你还记得竹林鸟夜莺耳机吗", ["owner"])
        self.assertEqual(hits[0]["id"], target)

    def test_state_job_no_change_and_conflict_do_not_inflate_score(self):
        from dataclasses import replace
        self.fx.service.settings = replace(self.fx.settings, state_updates=True)
        service = self.fx.service
        service.put_state("owner", {"relationship_score": 133})
        service.record_turn(local_env(), "你好", "")
        job = service.claim_job(kinds=["update_state"])
        service.complete_state_update(job["job"]["id"], {"relationship_delta": 0})
        self.assertEqual(service.get_state("owner")["version"], 1)
        service.record_turn(local_env(), "谢谢你", "")
        job = service.claim_job(kinds=["update_state"])
        service.put_state("owner", {"relationship_score": 134})
        with self.assertRaises(StateConflict):
            service.complete_state_update(job["job"]["id"], {"relationship_delta": 1},
                                          expected_version=job["materials"]["version"])
        self.assertEqual(service.get_state("owner")["data"]["relationship_score"], 134)
        self.assertEqual(self.fx.repo.get_job(job["job"]["id"])["status"], "running")

    def test_recalled_message_cancels_running_state_job(self):
        from dataclasses import replace
        self.fx.service.settings = replace(self.fx.settings, state_updates=True)
        service = self.fx.service
        service.put_state("owner", {"relationship_score": 133})
        service.record_message(local_env(), "user", "送你耳机", source_ref="recall-test")
        job = service.claim_job(kinds=["update_state"])
        service.record_message(local_env(), "user", "[消息已撤回]", source_ref="recall-test", recalled=True)
        self.assertEqual(service.complete_state_update(job["job"]["id"], {"relationship_delta": 1})["status"], "ignored")
        self.assertEqual(service.get_state("owner")["data"]["relationship_score"], 133)
        self.assertEqual(service.fail_job(job["job"]["id"], "迟到的模型异常")["status"], "done")
        self.assertIsNone(service.claim_job(kinds=["update_state"]))

    def test_desktop_manual_update_skips_background_state_work(self):
        from dataclasses import replace
        self.fx.service.settings = replace(self.fx.settings, state_updates=True)
        self.fx.service.record_turn(local_env(), "谢谢你", "我知道了", state_update=False)
        self.assertIsNone(self.fx.service.claim_job(kinds=["update_state"]))


class HybridRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()

    def tearDown(self):
        self.fx.close()

    def test_search_returns_original_message_evidence(self):
        self.fx.service.record_turn(local_env(self.fx.clock()), "我新买了一条蓝色围巾", "挺适合你")
        hits = self.fx.service.search("蓝色围巾", [OWNER_SCOPE], k=5)
        self.assertTrue(hits)
        evidence = [item for hit in hits for item in hit.get("evidence") or []]
        self.assertTrue(any(item["source_type"] == "message" and "蓝色围巾" in item["content"]
                            for item in evidence))
        self.assertTrue(all(item["scope"] == OWNER_SCOPE for item in evidence))

    def test_state_history_is_recorded_but_not_invented(self):
        self.fx.service.put_state(OWNER_SCOPE, {"relationship_score": 10})
        self.fx.service.put_state(OWNER_SCOPE, {"relationship_score": 11})
        history = self.fx.service.state_history(OWNER_SCOPE)
        self.assertEqual(history[0]["data"]["relationship_score"], 11)
        self.assertEqual(history[-1]["data"]["relationship_score"], 10)
        # 没有旧状态时不能凭空生成 version 0。
        self.assertFalse(any(item["version"] == 0 for item in history))

    def test_idle_small_conversation_flushes_summary_before_trigger(self):
        fx = MemoryFixture(summarize_trigger=40, summarize_chunk=30,
                           summarize_idle_seconds=60, summarize_idle_min=2)
        self.addCleanup(fx.close)
        fx.service.record_turn(local_env(ts=fx.clock()), "短会话第一句", "回复一")
        fx.service.record_turn(local_env(ts=fx.clock()), "短会话第二句", "回复二")
        fx.clock.advance(61)
        job = fx.service.claim_job(kinds=["summarize"])
        self.assertIsNotNone(job)
        self.assertEqual(job["job"]["kind"], "summarize")
        self.assertEqual(len(job["materials"]["messages"]), 4)

    def test_state_parser_tolerates_extra_json_but_rejects_empty_output(self):
        from memory.worker import _state_changes
        with self.assertRaisesRegex(ValueError, "empty text"):
            _state_changes("")
        self.assertEqual(_state_changes("{}"), {})
        self.assertEqual(_state_changes('{"relationship_delta":1}\n{"extra":2}'), {"relationship_delta": 1})
        self.assertEqual(_state_changes('结果如下：\n{"add_item":"围巾"}\n完成'), {"add_item": "围巾"})

    def test_fact_versions_keep_only_current_by_default(self):
        memory_id = self.fx.repo.add_memory(
            "semantic", OWNER_SCOPE, "管理员通常早晨跑步", subject_id="owner", meta={"episode_id": ""},
        )
        self.fx.repo.temporal.put_fact(memory_id, OWNER_SCOPE, "owner", "running_time", "morning", 10)
        second = self.fx.repo.add_memory(
            "semantic", OWNER_SCOPE, "管理员改为晚饭后慢跑", subject_id="owner", meta={"episode_id": ""},
        )
        self.fx.repo.temporal.put_fact(second, OWNER_SCOPE, "owner", "running_time", "evening", 20)
        current = self.fx.service.facts(OWNER_SCOPE, subject="owner", predicate="running_time")
        self.assertEqual([row["value"] for row in current], ["evening"])
        past = self.fx.service.facts(OWNER_SCOPE, subject="owner", predicate="running_time", as_of=15)
        self.assertEqual([row["value"] for row in past], ["morning"])


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()

    def tearDown(self):
        self.fx.close()

    def test_rebuild_from_sqlite_after_index_loss(self):
        for text in ("冰块在夏天最好吃", "螃蟹横着走路", "一起看动漫"):
            self.fx.service.add_memory("episode", OWNER_SCOPE, text)
        self.fx.index.reset()
        self.assertEqual(self.fx.index.count(), 0)
        result = self.fx.service.rebuild_index()
        self.assertEqual(result["total"], 3)
        hits = self.fx.service.search("螃蟹横着走", [OWNER_SCOPE], k=1)
        self.assertEqual(hits[0]["content"], "螃蟹横着走路")

    def test_deleted_memory_leaves_index(self):
        memory_id = self.fx.service.add_memory("episode", OWNER_SCOPE, "要被删除的记忆")["id"]
        self.fx.service.delete_memory(memory_id)
        self.assertEqual(self.fx.index.count(), 0)
        self.assertEqual(self.fx.service.search("要被删除的记忆", [OWNER_SCOPE]), [])

    def test_edit_reindexes(self):
        memory_id = self.fx.service.add_memory("semantic", OWNER_SCOPE, "旧内容")["id"]
        self.fx.service.update_memory(memory_id, content="管理员喜欢甜食")
        hits = self.fx.service.search("喜欢甜食", [OWNER_SCOPE], k=1)
        self.assertEqual(hits[0]["id"], memory_id)


if __name__ == "__main__":
    unittest.main()
