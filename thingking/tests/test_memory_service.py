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
        self.assertIsNone(facts["伪造的管理员事实"]["subject_id"])
        context = self.fx.service.build_context(qq_group_env(STRANGER_QQ), "篮球")
        self.assertIn("小明喜欢打篮球", [p["content"] for p in context["profile"]])

    def test_diary_job_for_live_episodes(self):
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
            def complete_state_update(inner, job_id, changes, expected_version=None):
                return service.complete_state_update(job_id, changes, expected_version)
            def fail_job(inner, job_id, error):
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
