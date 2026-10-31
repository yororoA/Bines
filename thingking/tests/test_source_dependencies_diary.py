"""Synthetic SQLite regressions for canonical lineage and incremental journals."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from helpers import Clock, local_env, qq_private_env
from memory.dependencies import direct_dependencies
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings


class SourceDependencyDiaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bines_dependency_test_")
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(1792800000)
        self.path = Path(self.tmp.name) / "test.sqlite3"
        self.settings = MemorySettings(data_dir=Path(self.tmp.name), state_updates=False,
            associations_enabled=False, association_backfill_batch=0, summarize_trigger=1000)
        self.repo = MemoryRepository(self.path, clock=self.clock)
        self.addCleanup(lambda: self.repo.close())
        self.start()

    def start(self):
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)
        self.service.sync_identities()

    def restart(self):
        self.repo.close()
        self.repo = MemoryRepository(self.path, clock=self.clock)
        self.start()

    def message(self, text="我住北京", ref="source", env=None):
        return self.service.record_message(env or local_env(self.clock()), "user", text,
                                           source_ref=ref)["message_ids"][0]

    def episode(self, day="2026-01-01", scope="owner", **meta):
        return self.repo.add_memory("episode", scope, "用户住北京", day_key=day, meta=meta)

    def children(self, mid, scope="owner"):
        return self.service.dependencies.descendants(scope, [("message", mid)])

    def test_correction_repairs_refs_ids_and_transitive_descendants_but_not_other_facts(self):
        mid = self.message()
        other = self.message("我喜欢蓝色", "other")
        ids = self.episode(source_message_ids=[mid])
        refs = self.episode(source_message_refs=["source"])
        child = self.repo.add_memory("diary", "owner", "用户住北京", meta={"episode_ids": [refs]})
        fact = self.repo.add_memory("semantic", "owner", "喜欢蓝色",
            meta={"episode_id": ids, "fact_source_message_id": other})
        foreign = self.episode(scope="qq_private:foreign", source_message_refs=["source"])
        for memory in (ids, refs, child, fact, foreign):
            self.service.graph.register_memory(memory)
        node = self.service.graph.for_message(mid)
        with patch.object(self.repo, "list_memories", side_effect=AssertionError("no full-memory scan")):
            result = self.service.correct_memory(scope="owner", node_id=node["id"], operation_id="fix",
                expected_digest=node["digest"], kind="fact", quote="北京", replacement="上海", explanation="纠正城市")
        self.assertEqual(set(result["repair_ids"]), {ids, refs, child})
        self.assertFalse(self.repo.get_memory(fact)["meta"].get("repair_pending"))
        self.assertFalse(self.repo.get_memory(foreign)["meta"].get("repair_pending"))
        job = self.repo.conn.execute("SELECT id FROM jobs WHERE kind='repair_memory' AND json_extract(payload,'$.memory_id')=?", (refs,)).fetchone()
        self.repo.conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job[0],))
        material = self.service.corrections.repair_materials(self.repo.get_job(job[0]))
        self.assertEqual(material["sources"][0]["id"], f"message:{mid}")
        self.assertEqual(material["sources"][0]["corrections"][0]["replacement"], "上海")

    def test_withdrawal_finds_ref_fact_and_nested_dependencies_without_global_scan(self):
        mid = self.message()
        episode = self.episode(source_message_refs=["source"])
        fact = self.repo.add_memory("semantic", "owner", "住北京", meta={"fact_source_message_id": mid})
        child = self.repo.add_memory("excerpt", "owner", "北京", meta={"source_memory_ids": [episode]})
        with patch.object(self.repo, "list_memories", side_effect=AssertionError("no full-memory scan")):
            self.service.record_message(local_env(self.clock()), "user", "[消息已撤回]", source_ref="source", recalled=True)
        self.assertTrue(all(self.repo.get_memory(mem)["deleted"] for mem in (episode, fact, child)))

    def test_direct_sql_change_rollback_and_restart_keep_reverse_index_correct(self):
        first = self.message(ref="first")
        second = self.message(ref="second")
        memory = self.episode(source_message_refs=["first"])
        self.assertEqual(self.children(first), {memory})
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.repo.transaction():
                self.repo.conn.execute("UPDATE memories SET meta=? WHERE id=?", (json.dumps({"source_message_refs": ["second"]}), memory))
                self.assertEqual(self.children(second), {memory})
                raise RuntimeError("rollback")
        self.assertEqual(self.children(first), {memory})
        self.assertEqual(self.children(second), set())
        self.repo.conn.execute("UPDATE memories SET meta=? WHERE id=?", (json.dumps({"source_message_refs": ["second"]}), memory))
        self.restart()
        self.assertEqual(self.children(first), set())
        self.assertEqual(self.children(second), {memory})

    def test_late_reference_implicit_episode_and_cycles_are_resolved(self):
        memory = self.episode(source_message_refs=["late"])
        self.service.dependencies.sync()
        mid = self.message(ref="late")
        implicit = self.episode()
        self.repo.conn.execute("UPDATE messages SET episode_id=? WHERE id=?", (implicit, mid))
        a = self.episode(source_memory_ids=[memory])
        b = self.episode(source_memory_ids=[a])
        self.repo.update_memory(a, meta={"source_memory_ids": [memory, b]})
        self.assertEqual(self.children(mid), {memory, implicit, a, b})
        self.repo.conn.execute("UPDATE messages SET episode_id=NULL WHERE id=?", (mid,))
        self.assertEqual(self.children(mid), {memory, a, b})
        self.repo.conn.execute("DELETE FROM memories WHERE id=?", (a,))
        self.assertEqual(self.children(mid), {memory})

    def test_scoped_aliases_and_message_ranges_share_the_same_parser(self):
        mid = self.message()
        memory = self.repo.add_memory("episode", "owner", "范围摘要", conversation_id="local",
                                      meta={"message_range": {"from_id": mid, "to_id": mid}})
        foreign = self.episode(scope="other", source_message_refs=["source"])
        self.assertIn(("message", str(mid)), direct_dependencies(self.repo, self.repo.get_memory(memory)))
        self.assertNotIn(("message", str(mid)), direct_dependencies(self.repo, self.repo.get_memory(foreign)))
        self.assertEqual(self.children(mid), {memory})

    def test_reverse_index_rebuild_restores_identical_results(self):
        mid = self.message()
        child = self.episode(source_message_refs=["source"])
        self.assertEqual(self.children(mid), {child})
        self.repo.conn.execute("DELETE FROM source_dependencies")
        self.service.dependencies.rebuild()
        self.assertEqual(self.children(mid), {child})
        self.repo.conn.execute("DROP TABLE source_dependencies")
        self.restart()
        self.assertEqual(self.children(mid), {child})

    def test_soft_delete_restore_reindexes_current_metadata(self):
        first = self.message(ref="first")
        second = self.message(ref="second")
        child = self.episode(source_message_refs=["first"])
        self.assertEqual(self.children(first), {child})
        self.repo.delete_memory(child)
        self.repo.conn.execute("UPDATE memories SET meta=? WHERE id=?", (json.dumps({"source_message_refs": ["second"]}), child))
        self.service.dependencies.sync()
        self.repo.conn.execute("UPDATE memories SET deleted=0 WHERE id=?", (child,))
        self.assertEqual(self.children(first), set())
        self.assertEqual(self.children(second), {child})

    def test_reference_only_memories_follow_binding_and_unbinding(self):
        env = qq_private_env("20002", self.clock())
        mid = self.message(ref="private", env=env)
        scope = "qq_private:20002"
        memory = self.episode(scope=scope, source_message_refs=["private"])
        self.repo.link_identity("qq", "20002", "owner")
        self.service.resolve(env)
        self.assertEqual(self.repo.get_memory(memory)["scope"], "owner")
        self.assertIn(memory, self.children(mid))
        self.repo.unlink_identities("qq", "owner", [])
        self.service.resolve(env)
        self.assertEqual(self.repo.get_memory(memory)["scope"], scope)
        self.assertIn(memory, self.children(mid, scope))
        self.assertEqual(self.children(mid), set())

    def test_completed_days_do_zero_work_until_a_specific_day_changes(self):
        episodes = []
        for i in range(90):
            day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
            mid = self.episode(day)
            episodes.append(mid)
            signature = self.service._episode_signature([self.repo.get_memory(mid)])
            self.repo.add_memory("diary", "owner", "已完成", day_key=day, meta=signature)
        for _ in range(4):
            self.assertEqual(self.service.enqueue_due_diaries(), 0)
        with patch.object(self.repo, "episodes_for_day", wraps=self.repo.episodes_for_day) as reads:
            self.repo.put_state("owner", {"current_location": "无关状态"})
            self.assertEqual(self.service.enqueue_due_diaries(), 0)
            self.assertEqual(reads.call_count, 0)
            self.repo.update_memory(episodes[41], content="该天新内容")
            self.assertEqual(self.service.enqueue_due_diaries(), 1)
            self.assertEqual(reads.call_count, 1)

    def test_legacy_backfill_is_bounded_resumable_and_preserves_existing_jobs(self):
        self.repo.close()
        self.path = Path(self.tmp.name) / "legacy.sqlite3"
        self.repo = MemoryRepository(self.path, clock=self.clock)
        for i in range(10):
            self.episode((date(2026, 1, 1) + timedelta(days=i)).isoformat())
        old_job = self.repo.enqueue_job("diary", "owner", {"day_key": "2026-01-01"}, "legacy-diary")
        self.settings = replace(self.settings, diary_backfill_batch=3, diary_maintenance_batch=2)
        self.start()
        self.assertLessEqual(self.service.enqueue_due_diaries(), 2)
        self.assertEqual(self.repo.conn.execute("SELECT value FROM meta WHERE key='diary_backfill_cursor'").fetchone()[0], "3")
        self.restart()
        self.assertEqual(self.repo.get_job(old_job)["status"], "pending")
        for _ in range(8):
            self.assertLessEqual(self.service.enqueue_due_diaries(), 2)
        days = {json.loads(row[0])["day_key"] for row in self.repo.conn.execute("SELECT payload FROM jobs WHERE kind='diary'")}
        self.assertEqual(len(days), 10)
        self.assertEqual(self.repo.conn.execute("SELECT value FROM meta WHERE key='diary_backfill_cursor'").fetchone()[0], "done")

    def test_dirty_days_follow_sql_moves_deletion_and_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.repo.transaction():
                self.episode()
                raise RuntimeError("rollback")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM diary_dirty_days").fetchone()[0], 0)
        mid = self.episode()
        self.repo.conn.execute("DELETE FROM diary_dirty_days")
        self.repo.conn.execute("UPDATE memories SET day_key='2026-01-02' WHERE id=?", (mid,))
        self.assertEqual({r[0] for r in self.repo.conn.execute("SELECT day_key FROM diary_dirty_days")}, {"2026-01-01", "2026-01-02"})
        self.repo.conn.execute("DELETE FROM diary_dirty_days")
        self.repo.conn.execute("UPDATE memories SET scope='private' WHERE id=?", (mid,))
        self.assertEqual(self.repo.conn.execute("SELECT day_key FROM diary_dirty_days").fetchone()[0], "2026-01-02")
        self.repo.conn.execute("DELETE FROM diary_dirty_days")
        self.repo.conn.execute("UPDATE memories SET scope='owner' WHERE id=?", (mid,))
        self.repo.conn.execute("DELETE FROM diary_dirty_days")
        self.repo.conn.execute("DELETE FROM memories WHERE id=?", (mid,))
        self.assertEqual(self.repo.conn.execute("SELECT day_key FROM diary_dirty_days").fetchone()[0], "2026-01-02")

    def test_current_day_is_retained_until_due_and_restart_does_not_lose_it(self):
        self.episode(self.service._day(self.clock()))
        self.assertEqual(self.service.enqueue_due_diaries(), 0)
        self.restart()
        self.clock.advance(2 * 86400)
        self.assertEqual(self.service.enqueue_due_diaries(), 1)

    def test_deleted_last_episode_retires_generated_diary_but_keeps_manual_diary(self):
        episode = self.episode()
        generated = self.repo.add_memory("diary", "owner", "生成日记", day_key="2026-01-01", meta={"episode_ids": [episode]})
        manual = self.repo.add_memory("diary", "owner", "人工日记", day_key="2026-01-01")
        self.repo.delete_memory(episode)
        self.assertEqual(self.service.enqueue_due_diaries(), 0)
        self.assertTrue(self.repo.get_memory(generated)["deleted"])
        self.assertFalse(self.repo.get_memory(manual)["deleted"])

    def test_replaced_identity_summary_does_not_permanently_block_diary(self):
        self.episode(repair_pending="identity:move", identity_rebuild_pending="qq_private:20002")
        inactive = self.episode(repair_pending="old-correction")
        self.repo.conn.execute("UPDATE memories SET origin='recalled' WHERE id=?", (inactive,))
        replacement = self.episode()
        self.assertEqual(self.service.enqueue_due_diaries(), 1)
        job = self.repo.conn.execute("SELECT payload FROM jobs WHERE kind='diary'").fetchone()
        self.assertEqual(json.loads(job[0])["episode_ids"], [replacement])

    def test_dependency_index_bootstraps_existing_rows_and_unrelated_writes_are_clean(self):
        self.repo.close()
        self.path = Path(self.tmp.name) / "old.sqlite3"
        self.repo = MemoryRepository(self.path, clock=self.clock)
        self.repo.ensure_conversation("local", "local", "owner")
        mid = self.repo.add_message("local", "owner", "user", "原话", self.clock(), "2026-01-01",
                                    actor_id="owner", actor_name="用户", source_ref="old")
        child = self.episode(source_message_refs=["old"])
        self.start()
        self.assertEqual(self.children(mid), {child})
        self.repo.put_state("owner", {"current_location": "上海"})
        self.repo.enqueue_job("unrelated", "owner", {}, "unrelated")
        self.assertEqual(self.service.dependencies.sync(), 0)


if __name__ == "__main__":
    unittest.main()
