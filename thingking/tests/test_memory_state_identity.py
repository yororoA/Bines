import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from helpers import Clock, local_env, qq_private_env
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings


class StateIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(1792800000)
        self.path = Path(self.tmp.name) / "memory.db"
        self.repo = MemoryRepository(self.path, self.clock)
        self.addCleanup(lambda: self.repo.close())
        self.settings = MemorySettings(data_dir=Path(self.tmp.name), state_updates=True,
                                       associations_enabled=False, association_backfill_batch=0,
                                       summarize_trigger=1000, job_max_model_calls=2,
                                       job_max_lease_recoveries=2)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)
        self.service.sync_identities()

    def record(self, text, env=None, ref=None):
        return self.service.record_message(env or local_env(self.clock()), "user", text, source_ref=ref)["message_ids"][0]

    def claim(self):
        return self.service.claim_job(["update_state"])

    def finish(self, claim, changes):
        return self.service.complete_state_update(claim["job"]["id"], changes,
            expected_version=claim["materials"]["version"], expected_attempt=claim["job"]["attempts"])

    def test_late_retry_filters_each_field_and_applies_delta_once(self):
        self.service.put_state("owner", {"relationship_score": 100})
        first_id = self.record("北京，获得钥匙并决定读书")
        first = self.claim()
        self.service.fail_job(first["job"]["id"], "temporary", expected_attempt=first["job"]["attempts"])
        self.clock.advance(1)
        self.record("到上海，钥匙已丢失")
        newer = self.claim()
        self.finish(newer, {"current_location": "上海", "remove_item": "钥匙", "relationship_delta": 1})
        self.clock.advance(61)
        retry = self.claim()
        self.finish(retry, {"current_location": "北京", "add_item": "钥匙", "active_quest": "读书", "relationship_delta": 1})
        data = self.service.get_state("owner")["data"]
        self.assertEqual((data["current_location"], data["active_quest"], data["relationship_score"]), ("上海", "读书", 102))
        self.assertNotIn("钥匙", data["inventory"])
        self.repo.enqueue_job("update_state", "owner", {"message_id": first_id}, "duplicate-effect", conversation_id="local")
        self.assertFalse(self.finish(self.claim(), {"relationship_delta": 1})["changed"])
        self.assertEqual(self.service.get_state("owner")["data"]["relationship_score"], 102)

    def test_same_timestamp_uses_message_order_and_survives_restart(self):
        self.record("北京")
        old = self.claim()
        self.service.fail_job(old["job"]["id"], "temporary")
        self.record("上海")
        self.finish(self.claim(), {"current_location": "上海"})
        self.repo.close()
        self.repo = MemoryRepository(self.path, self.clock)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)
        self.clock.advance(61)
        self.finish(self.claim(), {"current_location": "北京"})
        self.assertEqual(self.service.get_state("owner")["data"]["current_location"], "上海")

    def test_newer_manual_state_is_not_overwritten_by_delayed_automatic_field(self):
        self.record("北京")
        old = self.claim()
        self.service.fail_job(old["job"]["id"], "temporary")
        self.clock.advance(1)
        self.service.put_state("owner", {"current_location": "上海"})
        self.clock.advance(61)
        self.finish(self.claim(), {"current_location": "北京"})
        self.assertEqual(self.service.get_state("owner")["data"]["current_location"], "上海")

    def bind(self, enabled):
        self.service.settings = replace(self.settings, owner_qq_ids=frozenset({"20002"} if enabled else ()))
        self.service.sync_identities()
        return self.service.resolve(qq_private_env("20002", ts=self.clock()))

    def summarized_private(self):
        self.service.settings = replace(self.settings, state_updates=False, summarize_trigger=2, summarize_chunk=2)
        env = qq_private_env("20002", ts=self.clock())
        mid = self.record("我住北京", env, "private-source")
        self.service.record_message(env, "assistant", "收到", state_update=False)
        claim = self.service.claim_job(["summarize"])
        result = self.service.complete_summary(claim["job"]["id"], "用户居住北京",
            facts=[{"content": "用户居住北京", "subject": "qq:20002", "predicate": "city", "value": "北京", "source_message_id": mid}],
            expected_attempt=claim["job"]["attempts"])
        return env, mid, result

    def test_binding_moves_fact_subject_summary_graph_and_unbinding_restores_private_scope(self):
        env, mid, result = self.summarized_private()
        local = self.repo.add_memory("semantic", "owner", "本地管理员喜欢茶", subject_id="owner", conversation_id="local")
        for enabled, scope, actor in ((True, "owner", "owner"), (False, "qq_private:20002", "qq:20002")):
            self.bind(enabled)
            message = self.repo.get_message(mid)
            self.assertEqual((message["scope"], message["actor_id"]), (scope, actor))
            self.assertEqual(message["episode_id"], result["episode_id"])
            fact = self.repo.get_memory(result["semantic_ids"][0])
            self.assertEqual((fact["scope"], fact["subject_id"]), (scope, actor))
            self.assertEqual(self.service.facts(scope, subject=actor)[0]["value"], "北京")
            self.assertTrue(self.service.graph.valid(self.service.graph.for_memory(result["episode_id"])))
            self.assertEqual(self.repo.get_memory(local)["scope"], "owner")

    def test_state_migration_splits_shared_owner_effects_and_never_repeats_delta(self):
        self.service.put_state("owner", {"current_location": "本地", "relationship_score": 100})
        env = qq_private_env("20002", ts=self.clock())
        mid = self.record("私聊用户到北京", env, "private-source")
        self.finish(self.claim(), {"current_location": "北京", "relationship_delta": 1})
        self.bind(True)
        self.assertEqual(self.service.get_state("owner")["data"]["relationship_score"], 101)
        self.bind(False)
        owner, private = self.service.get_state("owner")["data"], self.service.get_state("qq_private:20002")["data"]
        self.assertEqual((owner["current_location"], owner["relationship_score"]), ("本地", 100))
        self.assertEqual((private["current_location"], private["relationship_score"]), ("北京", 1))
        self.assertFalse(any(h["data"].get("current_location") == "北京" for h in self.service.state_history("owner")))
        self.repo.enqueue_job("update_state", "qq_private:20002", {"message_id": mid}, "migration-retry", conversation_id="qq_private:20002")
        self.finish(self.claim(), {"relationship_delta": 1})
        self.assertEqual(self.service.get_state("qq_private:20002")["data"]["relationship_score"], 1)

    def test_inflight_state_result_is_fenced_when_identity_changes(self):
        self.record("北京", qq_private_env("20002", ts=self.clock()))
        claim = self.claim()
        self.bind(True)
        self.assertEqual(self.finish(claim, {"relationship_delta": 1})["status"], "ignored")
        self.finish(self.claim(), {"relationship_delta": 1})
        self.assertEqual(self.service.get_state("owner")["data"]["relationship_score"], 1)

    def test_mixed_derived_memory_is_hidden_and_transaction_rolls_back_on_failure(self):
        env, mid, result = self.summarized_private()
        self.bind(True)
        local = self.repo.add_memory("episode", "owner", "独立本地经历", conversation_id="local")
        mixed = self.repo.add_memory("diary", "owner", "混合日记", meta={"episode_ids": [local, result["episode_id"]]})
        self.service.graph.register_memory(mixed)
        self.service.settings = replace(self.settings, owner_qq_ids=frozenset())
        with patch.object(self.service.provenance, "migrate", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.service.sync_identities()
        self.assertEqual(self.repo.get_message(mid)["scope"], "owner")
        self.assertNotIn("repair_pending", self.repo.get_memory(mixed)["meta"])
        self.service.sync_identities()
        self.assertIn("identity_rebuild_pending", self.repo.get_memory(mixed)["meta"])
        self.assertEqual(self.repo.get_memory(local)["scope"], "owner")

    def test_unlink_restores_other_conversations_current_fact_interval(self):
        env, mid, result = self.summarized_private()
        local = self.repo.add_memory("semantic", "owner", "本地主人住上海", subject_id="owner", conversation_id="local")
        self.repo.temporal.put_fact(local, "owner", "owner", "city", "上海", self.clock() - 10)
        self.bind(True)
        self.assertEqual(self.service.facts("owner", predicate="city")[0]["value"], "北京")
        self.bind(False)
        self.assertEqual(self.service.facts("owner", predicate="city")[0]["value"], "上海")
        self.assertEqual(self.service.facts("qq_private:20002", predicate="city")[0]["value"], "北京")

    def test_old_sourced_state_history_moves_without_moving_unsourced_owner_fields(self):
        self.bind(True)
        env = qq_private_env("20002", ts=self.clock())
        mid = self.record("北京", env, "old-private-source")
        # Simulate a pre-upgrade database that has temporal history but no effects.
        self.repo.put_state("owner", {"active_quest": "本地任务"})
        self.repo.put_state("owner", {"active_quest": "本地任务", "current_location": "北京"}, source_ref=f"message:{mid}")
        self.repo.conn.execute("DELETE FROM state_effects")
        self.repo.conn.execute("DELETE FROM state_effect_baselines")
        self.repo.conn.execute("DELETE FROM state_field_clocks")
        self.bind(False)
        self.assertEqual(self.service.get_state("owner")["data"], {"active_quest": "本地任务"})
        self.assertEqual(self.service.get_state("qq_private:20002")["data"], {"current_location": "北京"})

    def test_repository_rejects_partial_scope_moves(self):
        self.repo.ensure_conversation("local", "local", "owner")
        with self.assertRaisesRegex(Exception, "source-aware"):
            self.repo.ensure_conversation("local", "local", "other")
        self.assertEqual(self.repo.conn.execute("SELECT scope FROM conversations WHERE id='local'").fetchone()[0], "owner")

    def test_exclusive_private_state_and_unlinked_memory_move_but_owner_baseline_stays(self):
        self.service.resolve(qq_private_env("20002"))
        self.service.put_state("qq_private:20002", {"current_location": "北京"})
        mid = self.repo.add_memory("semantic", "qq_private:20002", "用户喜欢蓝色", subject_id="qq:20002")
        self.bind(True)
        self.assertEqual(self.service.get_state("owner")["data"]["current_location"], "北京")
        self.assertEqual(self.repo.get_memory(mid)["scope"], "owner")
        self.bind(False)
        self.assertEqual(self.service.get_state("qq_private:20002")["data"]["current_location"], "北京")
        self.assertEqual(self.repo.get_memory(mid)["scope"], "qq_private:20002")

    def test_fact_corrections_follow_migrated_node_and_subject(self):
        env, mid, result = self.summarized_private()
        fact_id = result["semantic_ids"][0]
        node = self.service.graph.for_memory(fact_id)
        self.service.correct_memory(scope="qq_private:20002", node_id=node["id"], operation_id="correct-city",
            expected_digest=node["digest"], kind="fact", quote="北京", replacement="上海", explanation="合成测试纠错")
        self.bind(True)
        self.assertEqual(self.service.facts("owner", predicate="city")[0]["value"], "上海")
        self.assertTrue(self.service.graph.valid(self.service.graph.for_memory(fact_id)))
        self.bind(False)
        self.assertEqual(self.service.facts("qq_private:20002", predicate="city")[0]["value"], "上海")

    def test_late_delta_at_score_boundary_matches_rebuild_without_random_redraw(self):
        self.service.put_state("owner", {"relationship_score": 200})
        self.record("较早的正向事件")
        older = self.claim()
        self.service.fail_job(older["job"]["id"], "temporary")
        self.clock.advance(1)
        self.record("较新的负向事件")
        self.finish(self.claim(), {"relationship_delta": -1})
        self.clock.advance(61)
        self.finish(self.claim(), {"relationship_delta": 1})
        before = self.service.get_state("owner")["data"]
        self.assertEqual(before["relationship_score"], 199)
        with patch("random.random", side_effect=AssertionError("rebuild cannot redraw")):
            self.service.state_effects._rebuild("owner")
        self.assertEqual(self.service.get_state("owner")["data"], before)

    def test_identity_replay_excludes_missing_or_invalid_sources_but_keeps_scope_owned_manual_patch(self):
        self.service.put_state("owner", {"active_quest": "本地人工任务"})
        for index, origin in enumerate((None, "recalled", "withdrawn", "retracted", "deleted", "system")):
            with self.subTest(origin=origin):
                user = str(22000 + index)
                mid = self.record("私聊来源", qq_private_env(user, ts=self.clock()))
                self.finish(self.claim(), {"current_location": "不得重放的地点", "relationship_delta": 1})
                if origin is None:
                    self.repo.conn.execute("DELETE FROM messages WHERE id=?", (mid,))
                else:
                    self.repo.conn.execute("UPDATE messages SET origin=? WHERE id=?", (origin, mid))
                self.service.settings = replace(self.settings, owner_qq_ids=frozenset({user}))
                self.service.sync_identities()
                data = self.service.get_state("owner")["data"]
                self.assertEqual(data["active_quest"], "本地人工任务")
                self.assertNotIn("current_location", data)
                self.assertNotIn("relationship_score", data)

    def test_model_call_budget_prevents_the_provider_function_from_running(self):
        from common.model_usage import capture_model_usage, measured_request
        from types import SimpleNamespace
        self.record("测试")
        job = self.claim()["job"]
        calls = []
        def provider(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[], usage=None)
        def sink(event):
            return self.service.record_model_call(job["id"], event, job["attempts"])
        with capture_model_usage(sink):
            measured_request(provider, model="offline-fake")
            measured_request(provider, model="offline-fake")
            with self.assertRaisesRegex(ValueError, "budget"):
                measured_request(provider, model="offline-fake")
        self.assertEqual(len(calls), 2)

    def test_lease_recovery_budget_is_independent_of_generation_failures(self):
        self.record("测试")
        claims = [self.claim()]
        for _ in range(2):
            self.clock.advance(301)
            claims.append(self.claim())
        self.assertEqual([c["job"]["attempts"] for c in claims], [1, 2, 3])
        self.clock.advance(301)
        self.assertIsNone(self.claim())
        job = self.repo.get_job(claims[0]["job"]["id"])
        self.assertEqual((job["status"], job["lease_recoveries"]), ("failed", 2))
        self.assertEqual(job["payload"]["terminal_reason"], "lease_recovery_budget")
        self.assertNotIn("generation_failures", job["payload"])

    def test_model_budget_counts_started_unknown_and_stays_terminal_after_recovery(self):
        mid = self.record("测试")
        self.repo.enqueue_job("summarize", "owner", {"source_message_ids": [mid]}, "budget-summary", conversation_id="local")
        claim = self.service.claim_job(["summarize"])
        job = claim["job"]
        def record(number):
            return self.service.record_model_call(job["id"], {"call_id": f"{number:032x}", "status": "started",
                "started_at": self.clock(), "model": "fake"}, job["attempts"])
        self.assertEqual(record(1)["status"], "recorded")
        self.assertEqual(record(1)["status"], "duplicate")
        record(2)
        with self.assertRaisesRegex(ValueError, "budget"):
            record(3)
        self.assertEqual(self.repo.get_job(job["id"])["status"], "failed")
        self.clock.advance(7200)
        self.service.maintenance.tick()
        self.assertIsNone(self.service.claim_job(["summarize"]))
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memory_model_calls").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
