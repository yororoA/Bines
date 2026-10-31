"""Scoped facts, transactional source invalidation and bounded offline indexing."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import Clock, local_env, qq_group_env
from memory.evidence import EvidenceStore
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(1_792_800_000)
        self.path = Path(self.tmp.name) / "memory.db"
        self.repo = MemoryRepository(self.path, self.clock)
        self.addCleanup(lambda: self.repo.close())
        values = vars(MemorySettings(data_dir=Path(self.tmp.name), state_updates=False,
                                     associations_enabled=False, association_backfill_batch=0,
                                     summarize_trigger=2, summarize_chunk=2))
        self.settings = SimpleNamespace(**{**values, "index_batch_size": 2, "graph_expansion_enabled": True})
        self.service = MemoryService(self.repo, LexicalIndex(self.repo), self.settings, self.clock)
        self.keys = self.service.retriever.keys

    def memory(self, text, **kwargs):
        return self.repo.add_memory("episode", "owner", text, **kwargs)

    def sync(self):
        return self.keys.sync(repair_fts=False)


class IncrementalMemoryTest(MemoryCase):
    def test_unrelated_writes_and_index_marks_never_rescan_sources(self):
        mid = self.memory("记住蓝色围巾")
        self.sync()
        self.repo.put_state("owner", {"status": "awake"})
        self.repo.enqueue_job("associate", "owner", {}, "unrelated")
        self.repo.touch_memory(mid)
        self.repo.mark_indexed([mid])
        statements = []
        self.repo.conn.set_trace_callback(statements.append)
        with patch.object(self.keys, "_source_keys", side_effect=AssertionError("no dirty source")):
            self.service.search("围巾", ["owner"])
        self.repo.conn.set_trace_callback(None)
        self.assertFalse(any(sql.strip().upper().startswith("SELECT * FROM " + table)
                             and " WHERE " not in sql.upper()
                             for table in ("MEMORIES", "MESSAGES", "RETRIEVAL_KEYS") for sql in statements))

    def test_single_update_only_rebuilds_its_source(self):
        ids = [self.memory(f"独立记录{i}") for i in range(80)]
        self.sync()
        self.repo.conn.execute("UPDATE memories SET content=? WHERE id=?", ("改成唯一新关键词", ids[30]))
        with patch.object(self.keys, "_source_keys", wraps=self.keys._source_keys) as build:
            self.assertEqual(self.sync()["updated"], 1)
        self.assertEqual([(call.args[0]["id"], call.args[1]) for call in build.call_args_list], [(ids[30], "memory")])

    def test_optional_message_metadata_updates_are_tracked_before_and_after_schema_extension(self):
        for add_before_sync in (True, False):
            with self.subTest(add_before_sync=add_before_sync):
                repo = MemoryRepository(":memory:", self.clock)
                self.addCleanup(repo.close)
                store = EvidenceStore(repo)
                repo.ensure_conversation("local", "local", "owner")
                mid = repo.add_message("local", "owner", "user", "代称", self.clock(), "2026-10-25")
                if add_before_sync:
                    repo.conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT")
                    repo.conn.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps({"aliases": ["oldalias"]}), mid))
                store.sync(repair_fts=False)
                if not add_before_sync:
                    repo.conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT")
                repo.conn.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps({"aliases": ["newalias"]}), mid))
                self.assertEqual(store.sync(repair_fts=False)["updated"], 1)
                self.assertTrue(store.bm25_query("newalias", ["owner"]))
                self.assertFalse(store.bm25_query("oldalias", ["owner"]))

    def test_existing_trigger_definition_is_upgraded_and_repairs_missed_updates(self):
        mid = self.memory("需要升级触发器")
        self.sync()
        name = "retrieval_dirty_memories_au"
        statement = self.repo.conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
        self.repo.conn.execute(f"DROP TRIGGER {name}")
        self.repo.conn.execute(statement.replace(",meta,", ","))
        self.repo.conn.execute("UPDATE memories SET meta=? WHERE id=?", (json.dumps({"aliases": ["missedalias"]}), mid))
        self.assertFalse(self.repo.conn.execute("SELECT 1 FROM retrieval_dirty_sources").fetchone())
        self.assertEqual(self.sync()["updated"], 1)
        self.assertTrue(self.keys.bm25_query("missedalias", ["owner"]))

    def test_dirty_queue_survives_restart_and_external_sql(self):
        mid = self.memory("旧版本")
        self.sync()
        self.repo.close()
        with sqlite3.connect(self.path) as other:
            other.execute("UPDATE memories SET content=? WHERE id=?", ("重启后的新版本", mid))
        self.repo = MemoryRepository(self.path, self.clock)
        store = EvidenceStore(self.repo)
        self.assertEqual(store.sync(repair_fts=False)["updated"], 1)
        self.assertEqual(store.get_keys([f"m:{mid}:0"], ["owner"])[f"m:{mid}:0"]["content"], "重启后的新版本")

    def test_sync_and_dirty_records_roll_back_with_source_transaction(self):
        mid = self.memory("事务前")
        self.sync()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.repo.transaction():
                self.repo.conn.execute("UPDATE memories SET content='不应保留' WHERE id=?", (mid,))
                self.sync()
                raise RuntimeError("rollback")
        self.assertEqual(self.repo.get_memory(mid)["content"], "事务前")
        self.assertEqual(self.keys.get_keys([f"m:{mid}:0"], ["owner"])[f"m:{mid}:0"]["content"], "事务前")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM retrieval_dirty_sources").fetchone()[0], 0)
        self.repo.conn.execute("UPDATE memories SET content='真正的新版本' WHERE id=?", (mid,))
        self.assertEqual(self.sync()["updated"], 1)

    def test_legacy_database_gets_one_initial_seed_then_stays_incremental(self):
        repo = MemoryRepository(":memory:", self.clock)
        self.addCleanup(repo.close)
        mid = repo.add_memory("episode", "owner", "旧库原文")
        store = EvidenceStore(repo)
        self.assertEqual(store.sync()["added"], 1)
        # Simulate a database created before the dirty-source schema, with an
        # existing derived index but no change tracking.
        triggers = [r[0] for r in repo.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'retrieval_dirty_%'")]
        for trigger in triggers:
            repo.conn.execute(f'DROP TRIGGER "{trigger}"')
        repo.conn.execute("DELETE FROM retrieval_sync_state WHERE key='dirty_sources_v1'")
        repo.conn.execute("UPDATE memories SET content='离线修改过的旧库' WHERE id=?", (mid,))
        restored = EvidenceStore(repo)
        self.assertEqual(restored.sync()["updated"], 1)
        with patch.object(restored, "_source_keys", side_effect=AssertionError("seed repeated")):
            self.assertEqual(restored.sync()["updated"], 0)

    def test_raw_retraction_invalidates_transitive_dependents_and_restoration_requeues(self):
        env = local_env(self.clock())
        mid = self.service.record_message(env, "user", "我住在北京", state_update=False)["message_ids"][0]
        child = self.memory("首都居民", meta={"source_message_ids": [mid]})
        grandchild = self.memory("记录了居住信息", meta={"source_memory_ids": [child]})
        for memory in (child, grandchild):
            self.service.graph.register_memory(memory)
        self.sync()
        ids = [f"m:{child}:0", f"m:{grandchild}:0"]
        self.assertEqual(len(self.keys.get_keys(ids, ["owner"])), 2)
        self.repo.conn.execute("UPDATE messages SET origin='recalled',content='[消息已撤回]' WHERE id=?", (mid,))
        self.sync()
        self.assertFalse(self.keys.get_keys(ids, ["owner"]))
        self.assertFalse(self.repo.conn.execute("SELECT 1 FROM retrieval_keys WHERE memory_id IN (?,?)", (child, grandchild)).fetchone())
        self.repo.conn.execute("UPDATE messages SET origin='live',content='我住在北京' WHERE id=?", (mid,))
        self.sync()
        self.assertEqual(len(self.keys.get_keys(ids, ["owner"])), 2)

    def test_source_change_revisits_dependents_and_matches_full_rebuild(self):
        env = local_env(self.clock())
        mid = self.service.record_message(env, "user", "旧版原话", state_update=False)["message_ids"][0]
        child = self.memory("原话的摘要", meta={"source_message_ids": [mid]})
        grandchild = self.memory("摘要的摘要", meta={"source_memory_ids": [child]})
        self.sync()
        self.repo.conn.execute("UPDATE messages SET content='新版原话' WHERE id=?", (mid,))
        with patch.object(self.keys, "_source_keys", wraps=self.keys._source_keys) as build:
            self.sync()
        rebuilt = {(call.args[1], str(call.args[0]["id"])) for call in build.call_args_list}
        self.assertIn(("memory", child), rebuilt)
        self.assertIn(("memory", grandchild), rebuilt)
        before = [tuple(r) for r in self.repo.conn.execute("SELECT * FROM retrieval_keys ORDER BY id")]
        self.keys.sync(rebuild=True)
        self.assertEqual(before, [tuple(r) for r in self.repo.conn.execute("SELECT * FROM retrieval_keys ORDER BY id")])

    def test_retention_change_reindexes_only_affected_node(self):
        mid = self.memory("冷热层测试")
        node = self.service.graph.register_memory(mid)
        self.sync()
        self.repo.conn.execute("UPDATE association_nodes SET retrieval_status='cold' WHERE id=?", (node["id"],))
        self.assertEqual(self.sync()["deleted"], 1)
        self.repo.conn.execute("UPDATE association_nodes SET retrieval_status='hot' WHERE id=?", (node["id"],))
        self.assertEqual(self.sync()["added"], 1)

    def test_experience_sql_update_changes_keys_and_rejects_stale_fingerprint(self):
        mid = self.memory("网页经历", meta={"experience_ref": "test:experience"})
        self.repo.conn.execute("INSERT INTO experiences VALUES(?,?,?,?,?,?)",
                              ("test:experience", mid, "owner", json.dumps({"result": "old unique"}), 1, self.clock()))
        self.sync()
        key = f"m:{mid}:0"
        self.keys.mark_indexed([key])
        self.repo.conn.execute("UPDATE experiences SET record=? WHERE source_ref=?",
                              (json.dumps({"result": "new unique"}), "test:experience"))
        self.assertFalse(self.keys.get_keys([key], ["owner"]))
        self.sync()
        row = self.keys.get_keys([key], ["owner"])[key]
        self.assertIn("new unique", row["content"])
        self.assertIsNone(row["indexed_at"])

    def test_graph_expansion_switch_preserves_direct_graph_path(self):
        self.settings.associations_enabled = True
        mid = self.memory("绿色围巾约定")
        self.service.graph.register_memory(mid)
        self.settings.graph_expansion_enabled = False
        with patch.object(self.service.graph, "expand", side_effect=AssertionError("expansion disabled")):
            result = self.service.search("围巾", ["owner"])
        self.assertEqual(result[0]["memory_id"], mid)
        self.assertIn("node_id", result[0])
        self.assertFalse(self.service.retriever.last_trace["graph_expansion"])
        self.settings.graph_expansion_enabled = True
        with patch.object(self.service.graph, "expand", return_value=[]) as expand:
            self.assertEqual([r["id"] for r in self.service.search("围巾", ["owner"])], [r["id"] for r in result])
        expand.assert_called_once()


class FactAttributionTest(MemoryCase):
    def finish(self, facts, *, two_speakers=True):
        first = self.service.record_message(qq_group_env("111", ts=self.clock()), "user", "我喜欢红茶", state_update=False)["message_ids"][0]
        second = self.service.record_message(qq_group_env("222" if two_speakers else "111", ts=self.clock()), "user", "我是另一个人", state_update=False)["message_ids"][0]
        claim = self.service.claim_job(["summarize"])
        result = self.service.complete_summary(claim["job"]["id"], "群成员聊天", facts=facts(first, second), expected_attempt=claim["job"]["attempts"])
        return [self.repo.get_memory(mid) for mid in result["semantic_ids"]]

    def test_rejects_fact_citing_another_speaker(self):
        self.assertEqual(self.finish(lambda first, _: [{"subject": "qq:222", "content": "第二位用户喜欢红茶", "source_message_id": first}]), [])

    def test_correct_citation_binds_fact_to_its_speaker(self):
        facts = self.finish(lambda first, _: [{"subject": "qq:111", "content": "第一位用户喜欢红茶", "source_message_id": first,
                                                "predicate": "favorite_drink", "value": "红茶"}])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["subject_id"], "qq:111")
        self.assertIsNotNone(facts[0]["meta"]["fact_source_message_id"])

    def test_legacy_missing_source_is_rejected_for_multiple_speakers(self):
        self.assertEqual(self.finish(lambda *_: [{"subject": "qq:111", "content": "喜欢红茶"}]), [])

    def test_legacy_unversioned_single_speaker_is_preserved(self):
        facts = self.finish(lambda *_: [{"subject": "qq:111", "content": "喜欢红茶"}], two_speakers=False)
        self.assertEqual(facts[0]["subject_id"], "qq:111")

    def test_versioned_fact_never_infers_a_missing_source(self):
        self.assertEqual(self.finish(lambda *_: [{"subject": "qq:111", "content": "喜欢红茶", "predicate": "drink", "value": "红茶"}], two_speakers=False), [])


class RecordingIndex:
    persistent = True

    def __init__(self):
        self.memories, self.keys = {}, {}
        self.memory_batches, self.key_batches, self.delete_batches = [], [], []
        self.inventories = 0

    def upsert(self, rows):
        self.memory_batches.append(len(rows))
        self.memories.update((r["id"], r["content"]) for r in rows)
        return [r["id"] for r in rows]

    def upsert_keys(self, rows):
        self.key_batches.append(len(rows))
        self.keys.update((r["id"], r["content"]) for r in rows)
        return [r["id"] for r in rows]

    def key_ids(self):
        self.inventories += 1
        return list(self.keys)

    def delete_keys(self, ids):
        self.delete_batches.append(len(ids))
        for key in ids:
            self.keys.pop(key, None)

    def delete(self, ids):
        for key in ids:
            self.memories.pop(key, None)

    def reset(self):
        self.memories.clear()
        self.keys.clear()

    def count(self):
        return len(self.memories)


class BoundedIndexTest(MemoryCase):
    def setUp(self):
        super().setUp()
        self.index = RecordingIndex()
        self.service.index = self.service.retriever.index = self.index

    def test_worker_poll_indexes_one_batch_and_explicit_maintenance_drains(self):
        for i in range(7):
            self.memory(f"记忆 {i}")
        self.service.claim_job(["summarize"])
        self.assertEqual(self.index.memory_batches, [2])
        self.assertEqual(self.index.key_batches, [2])
        self.assertEqual(len(self.repo.unindexed_memories()), 5)
        self.service.reconcile_index()
        self.assertEqual(len(self.index.memories), 7)
        self.assertEqual(len(self.index.keys), 7)
        self.assertTrue(all(n <= 2 for n in self.index.memory_batches + self.index.key_batches))
        self.assertEqual(self.index.inventories, 1)

    def test_deletions_and_recreated_keys_do_not_erase_fresh_vectors(self):
        mid = self.memory("旧索引")
        self.service.reconcile_index()
        key = f"m:{mid}:0"
        self.repo.conn.execute("DELETE FROM retrieval_keys WHERE id=?", (key,))
        self.repo.conn.execute("UPDATE memories SET content='新索引' WHERE id=?", (mid,))
        self.service.reconcile_index(full=False)
        self.service.reconcile_index(full=False)
        self.assertEqual(self.index.keys[key], "新索引")
        self.assertFalse(self.repo.conn.execute("SELECT 1 FROM retrieval_vector_deletions").fetchone())
        self.assertEqual(self.index.inventories, 1)

    def test_backlog_progress_is_bounded_and_does_not_wait_for_next_diary(self):
        for i in range(7):
            self.memory(f"待处理 {i}")
        self.service.claim_job(["summarize"])
        self.clock.advance(4)
        self.service.claim_job(["summarize"])
        self.assertEqual(self.index.key_batches, [2])
        self.clock.advance(1)
        with patch.object(self.service.graph, "backfill", side_effect=AssertionError("not diary maintenance")):
            self.service.claim_job(["summarize"])
        self.assertEqual(self.index.key_batches, [2, 2])
        self.assertEqual(self.index.memory_batches, [2, 2])

    def test_rebuild_restores_every_vector_in_bounded_requests(self):
        for i in range(5):
            self.memory(f"重建 {i}")
        self.service.reconcile_index(full=False)
        result = self.service.rebuild_index()
        self.assertEqual(result["total"], 5)
        self.assertEqual(len(self.index.keys), 5)
        self.assertTrue(all(n <= 2 for n in self.index.memory_batches + self.index.key_batches))
