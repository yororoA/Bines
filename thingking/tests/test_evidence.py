"""Offline evidence-store tests. Every database is in-memory, never production."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memory.evidence import CHUNK_OVERLAP, CHUNK_SIZE, EvidenceStore
from memory.repository import MemoryRepository


class EvidenceStoreTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.repo = MemoryRepository(":memory:", clock=lambda: self.now)
        self.addCleanup(self.repo.close)
        self.store = EvidenceStore(self.repo)

    def memory(self, content="存量摘要", scope="owner", **kwargs):
        return self.repo.add_memory(kwargs.pop("kind", "episode"), scope, content, **kwargs)

    def message(self, content="原始消息", scope="owner", conversation_id=None, **kwargs):
        conversation_id = conversation_id or f"conversation:{scope}"
        self.repo.ensure_conversation(conversation_id, "local", scope)
        return self.repo.add_message(
            conversation_id, scope, kwargs.pop("role", "user"), content, kwargs.pop("ts", self.now),
            "2026-10-22", actor_id=kwargs.pop("actor_id", "actor:1"), **kwargs,
        )

    def key(self, source_id, source_type="memory", chunk=0):
        return f"{'m' if source_type == 'memory' else 'u'}:{source_id}:{chunk}"

    def source_snapshot(self):
        return {
            table: [tuple(row) for row in self.repo.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("meta", "memories", "messages", "conversations", "state", "jobs", "migration_ledger")
        }

    def fts_ids(self):
        return {row["id"] for row in self.repo.conn.execute("SELECT id FROM retrieval_keys_fts")}

    def test_schema_is_independent_and_construction_does_not_sync(self):
        memory_id = self.memory()
        original = self.source_snapshot()
        self.store.ensure_schema()
        self.assertEqual(self.store.all_ids(), [])
        self.assertEqual(self.source_snapshot(), original)
        stats = self.store.sync()
        self.assertEqual(stats, dict(added=1, updated=0, deleted=0, unchanged=0, total=1, fts_repaired=0))
        key_id = self.key(memory_id)
        row = self.store.get_keys([key_id], ["owner"])[key_id]
        for name in ("id", "scope", "kind", "memory_id", "message_id", "content", "search_text",
                     "source_hash", "indexed_at"):
            self.assertIn(name, row)
        self.assertEqual(row["meta"]["source_type"], "memory")
        self.assertEqual(row["meta"]["source_id"], memory_id)
        self.assertEqual(self.source_snapshot(), original)

    def test_sync_is_idempotent_and_vector_marks_are_independent(self):
        memory_id = self.memory()
        message_id = self.message()
        self.repo.mark_indexed([memory_id])
        self.store.sync()
        ids = [self.key(memory_id), self.key(message_id, "message")]
        self.assertEqual(set(self.store.all_ids()), set(ids))
        self.assertEqual(len(self.store.unindexed_keys(limit=1)), 1)
        self.assertEqual(self.store.unindexed_keys(limit=0), [])
        self.store.mark_indexed([*ids, "missing"])
        self.assertEqual(self.store.unindexed_keys(), [])
        self.now += 100
        self.repo.touch_memory(memory_id)
        stats = self.store.sync()
        self.assertEqual(stats["unchanged"], 2)
        self.assertEqual(stats["added"] + stats["updated"] + stats["deleted"], 0)
        self.assertEqual({row["indexed_at"] for row in self.store.get_keys(ids, ["owner"]).values()}, {1000})
        self.store.clear_index_marks()
        self.assertEqual(len(self.store.unindexed_keys()), 2)
        self.assertEqual(self.repo.get_memory(memory_id)["indexed_at"], 1000)

    def test_paragraphs_and_long_sources_have_stable_overlapping_chunks(self):
        text = "甲" * 701 + "乙" * 799
        memory_id = self.memory(text)
        message_id = self.message("首段\n\n" + text)
        before = self.source_snapshot()
        self.store.sync()
        rows = self.store.get_keys(self.store.all_ids(), ["owner"])
        memory_chunks = sorted((row for row in rows.values() if row["memory_id"] == memory_id),
                               key=lambda row: row["chunk_index"])
        message_chunks = sorted((row for row in rows.values() if row["message_id"] == message_id),
                                key=lambda row: row["chunk_index"])
        self.assertTrue(all(0 < len(row["content"]) <= CHUNK_SIZE for row in rows.values()))
        self.assertEqual(message_chunks[0]["content"], "首段")
        self.assertEqual(memory_chunks[0]["content"][-CHUNK_OVERLAP:],
                         memory_chunks[1]["content"][:CHUNK_OVERLAP])
        self.assertEqual(memory_chunks[0]["content"] + "".join(
            row["content"][CHUNK_OVERLAP:] for row in memory_chunks[1:]), text)
        ids = self.store.all_ids()
        self.assertEqual(self.store.sync()["unchanged"], len(ids))
        self.assertEqual(self.store.all_ids(), ids)
        self.assertEqual(self.source_snapshot(), before)
        bounded = self.store.evidence(message_chunks[-1], ["owner"])[0]["content"]
        self.assertLessEqual(len(bounded), 480)
        self.assertIn(bounded, "首段\n\n" + text)

    def test_changes_prune_old_chunks_and_fts_and_reset_marks(self):
        memory_id = self.memory("旧关键词" * 400)
        message_id = self.message("旧消息")
        self.store.sync()
        old_ids = self.store.all_ids()
        self.store.mark_indexed(old_ids)
        self.repo.update_memory(memory_id, content="新版本 Python")
        self.repo.conn.execute("UPDATE messages SET content = ? WHERE id = ?", ("新消息 Rust", message_id))
        self.assertEqual(self.store.get_keys(old_ids, ["owner"]), {})
        self.assertEqual(self.store.bm25_query("旧关键词", ["owner"]), [])
        self.assertEqual(self.store.all_ids(), [])
        self.store.mark_indexed(old_ids)
        stats = self.store.sync()
        self.assertEqual(stats["updated"], 2)
        self.assertEqual(stats["deleted"], len(old_ids) - 2)
        self.assertEqual(len(self.store.unindexed_keys()), 2)
        self.assertEqual(self.fts_ids(), set(self.store.all_ids()))
        self.assertEqual(self.store.bm25_query("旧", ["owner"]), [])
        self.assertEqual(self.store.bm25_query("Python", ["owner"])[0][0], self.key(memory_id))
        self.assertEqual(self.store.bm25_query("Rust", ["owner"])[0][0], self.key(message_id, "message"))

    def test_exact_whitespace_changes_are_not_hidden_by_repository_hash(self):
        memory_id = self.memory("a b")
        self.store.sync()
        old_hash = self.repo.get_memory(memory_id)["content_hash"]
        self.repo.update_memory(memory_id, content="a  b")
        self.assertEqual(self.repo.get_memory(memory_id)["content_hash"], old_hash)
        self.assertEqual(self.store.get_keys([self.key(memory_id)], ["owner"]), {})
        self.assertEqual(self.store.sync()["updated"], 1)
        self.assertEqual(self.store.evidence(self.key(memory_id), ["owner"])[0]["content"], "a  b")

    def test_metadata_expansion_is_searchable_and_has_provenance(self):
        memory_id = self.memory(
            "他也喜欢这个", kind="semantic", subject_id="actor:lin", tags=["项目"],
            meta={"retrieval": {"context": "林先生在讨论数据库", "keywords": ["SQLite"]},
                  "context": "中文全文搜索", "aliases": {"林先生": ["小林", "Lin"]},
                  "entities": ["数据库"], "keywords": ["Bines"]},
        )
        self.store.sync()
        key_id = self.key(memory_id)
        row = self.store.get_keys([key_id], ["owner"])[key_id]
        self.assertIn("meta.retrieval.context", row["meta"]["expansion_sources"])
        self.assertIn("meta.aliases", row["meta"]["expansion_sources"])
        for query in ("小林", "林先生", "数据库", "Lin", "SQLite", "全文", "Bines", "项目"):
            with self.subTest(query=query):
                self.assertEqual(self.store.bm25_query(query, ["owner"])[0][0], key_id)
        self.store.mark_indexed([key_id])
        self.repo.update_memory(memory_id, meta={"keywords": ["Postgres"]})
        self.assertEqual(self.store.get_keys([key_id], ["owner"]), {})
        self.assertEqual(self.store.sync()["updated"], 1)
        self.assertEqual(self.store.bm25_query("SQLite", ["owner"]), [])
        self.assertEqual(self.store.bm25_query("Postgres", ["owner"])[0][0], key_id)
        self.assertEqual(self.store.unindexed_keys()[0]["content"], "他也喜欢这个")

    def test_optional_message_metadata_and_malformed_metadata_are_safe(self):
        # Some importers may provide message metadata; current repository does not.
        self.repo.conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT")
        message_id = self.message("代称")
        self.repo.conn.execute("UPDATE messages SET meta = ? WHERE id = ?", (
            json.dumps({"retrieval": {"aliases": ["别名"]}, "context": "背景"}, ensure_ascii=False), message_id,
        ))
        bad_id = self.memory("有效文本")
        self.repo.conn.execute("UPDATE memories SET meta = 'not-json' WHERE id = ?", (bad_id,))
        self.store.sync()
        self.assertEqual(self.store.bm25_query("别名", ["owner"])[0][0], self.key(message_id, "message"))
        self.assertIn(self.key(bad_id), self.store.all_ids())
        self.assertEqual(self.store.evidence(self.key(bad_id), ["owner"])[0]["source_type"], "summary")

    def test_chinese_unigrams_bigrams_and_english_whole_words(self):
        memory_id = self.memory("我住在杭州，使用Python开发 SQLite工具")
        self.store.sync()
        key_id = self.key(memory_id)
        for query in ("杭", "杭州", "开发", "PYTHON", "sqlite"):
            self.assertEqual(self.store.bm25_query(query, ["owner"])[0][0], key_id)
        self.assertEqual(self.store.bm25_query("Pyth", ["owner"]), [])
        tokens = self.repo.conn.execute("SELECT tokens FROM retrieval_keys_fts").fetchone()[0].split()
        self.assertIn("杭", tokens)
        self.assertIn("杭州", tokens)
        self.assertIn("python", tokens)
        for query in ("", " !!! ", '" OR * : ()', "\n"):
            self.assertEqual(self.store.bm25_query(query, ["owner"]), [])

    def test_authorization_and_kind_filtering_happen_before_ranking_limit(self):
        for _ in range(45):
            self.memory("SQLite", scope="secret")
        visible = self.memory("公共话题 " * 100 + "SQLite", scope="group", kind="semantic")
        message_id = self.message("SQLite", scope="group")
        self.store.sync()
        hits = self.store.bm25_query("SQLite", ["group"], kinds=["semantic"], limit=1)
        self.assertEqual([key_id for key_id, _ in hits], [self.key(visible)])
        self.assertIsInstance(hits[0][1], float)
        self.assertLess(hits[0][1], 0)
        self.assertEqual(self.store.bm25_query("SQLite", ["group"], kinds=["message"], limit=1)[0][0],
                         self.key(message_id, "message"))
        self.assertEqual(len(self.store.get_keys(self.store.all_ids(), ["group"])), 2)
        self.assertEqual(self.store.bm25_query("SQLite", [], limit=1), [])
        self.assertEqual(self.store.bm25_query("SQLite", ["group"], kinds=[]), [])
        self.assertEqual(self.store.bm25_query("SQLite", ["group"], limit=0), [])
        self.assertEqual(self.store.get_keys(self.store.all_ids(), []), {})
        self.assertEqual(self.store.get_keys([], ["owner"]), {})
        self.assertEqual(self.store.bm25_query("SQLite", ["group') OR 1=1 --"]), [])

    def test_stale_hits_do_not_consume_the_limit(self):
        stale = [self.memory("SQLite") for _ in range(405)]
        valid = self.memory("背景内容 " * 20 + "SQLite")
        self.store.sync()
        for memory_id in stale:
            self.repo.update_memory(memory_id, content="已经变更")
        self.assertEqual(self.store.bm25_query("SQLite", ["owner"], limit=1)[0][0], self.key(valid))
        self.assertEqual([row["id"] for row in self.store.unindexed_keys(limit=1)], [self.key(valid)])

    def test_inactive_sources_are_excluded_and_fts_entries_are_removed(self):
        for text in ("", " \n\t", "[消息已撤回]", "[System Event: timer]", "[系统：通知]", "[screen monitor idle]"):
            self.memory(text)
            self.message(text)
        self.message("系统消息", role="system")
        self.memory("元数据撤回", meta={"recalled": True})
        self.message("已删除", origin="deleted")
        memory_id = self.memory("保留记忆")
        message_id = self.message("保留消息")
        self.store.sync()
        ids = [self.key(memory_id), self.key(message_id, "message")]
        self.assertEqual(set(self.store.all_ids()), set(ids))
        self.repo.delete_memory(memory_id)
        self.repo.conn.execute("UPDATE messages SET content = '[消息已撤回]' WHERE id = ?", (message_id,))
        self.assertEqual(self.store.get_keys(ids, ["owner"]), {})
        self.assertEqual(self.store.evidence(ids[1], ["owner"]), [])
        self.assertEqual(self.store.sync()["deleted"], 2)
        self.assertEqual(self.fts_ids(), set())

    def test_physical_deletion_and_scope_moves_invalidate_before_sync(self):
        memory_id = self.memory("唯一记忆")
        message_id = self.message("唯一消息")
        self.store.sync()
        old_key = self.store.get_keys([self.key(message_id, "message")], ["owner"])[self.key(message_id, "message")]
        self.repo.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.repo.ensure_conversation("conversation:owner", "local", "new_scope")
        self.assertEqual(self.store.get_keys(self.store.all_ids(), ["owner"]), {})
        self.assertEqual(self.store.evidence(old_key, ["owner", "new_scope"]), [])
        self.assertEqual(self.store.bm25_query("唯一", ["owner"]), [])
        stats = self.store.sync()
        self.assertEqual(stats["deleted"], 1)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(self.store.evidence(old_key, ["owner"]), [])
        self.assertEqual(self.store.evidence(old_key, ["new_scope"])[0]["scope"], "new_scope")
        self.repo.conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        self.assertEqual(self.store.all_ids(), [])
        self.assertEqual(self.store.sync()["deleted"], 1)
        self.assertEqual(self.fts_ids(), set())

    def test_message_evidence_returns_verbatim_event_not_expanded_key(self):
        message_id = self.message(
            "  实际发言\n保留换行  ", role="observed", actor_id="qq:123", actor_name="检索昵称",
            source_ref="qq:message:555", ts=42,
        )
        self.store.sync()
        key_id = self.key(message_id, "message")
        row = self.store.get_keys([key_id], ["owner"])[key_id]
        self.assertIn("检索昵称", row["search_text"])
        row.update(content="伪造原文", scope="secret", message_id=999)
        self.assertEqual(self.store.evidence(row, ["owner"]), [{
            "source_type": "message", "source_id": message_id, "source_ref": "qq:message:555",
            "role": "observed", "actor_id": "qq:123", "content": "  实际发言\n保留换行  ",
            "ts": 42, "scope": "owner",
        }])
        self.assertEqual(self.store.evidence(row, []), [])
        self.assertEqual(self.store.evidence(row, ["owner"], limit=0), [])
        self.assertEqual(self.store.evidence("missing", ["owner"]), [])

    def test_message_range_resolves_raw_events_with_query_and_limit(self):
        first = self.message("第一句普通内容", ts=1)
        secret = self.message("杭州私密内容", scope="secret", ts=2)
        other = self.message("杭州另一场会话", conversation_id="other", ts=3)
        selected = self.message("杭州原始行程", role="assistant", ts=4)
        last = self.message("最后一句普通内容", ts=5)
        memory_id = self.memory("可能有偏差的行程摘要", conversation_id="conversation:owner",
                                meta={"message_range": [first, last]})
        self.store.sync()
        rows = self.store.evidence(self.key(memory_id), ["owner", "secret"], query="杭州", limit=2)
        self.assertEqual(rows[0]["source_id"], selected)
        self.assertEqual(rows[0]["role"], "assistant")
        self.assertEqual(rows[0]["content"], "杭州原始行程")
        self.assertEqual(len(rows), 2)
        self.assertFalse({secret, other} & {row["source_id"] for row in rows})
        self.assertTrue(all(row["source_type"] == "message" for row in rows))

    def test_episode_reference_follows_range_and_direct_episode_links(self):
        message_id = self.message("事实的原始来源")
        episode_id = self.memory("摘要", conversation_id="conversation:owner",
                                 meta={"message_range": [message_id, message_id]})
        fact_id = self.memory("事实", kind="semantic", meta={"episode_id": episode_id})
        direct_id = self.message("通过 episode_id 找回的消息", episode_id=episode_id, ts=2)
        self.store.sync()
        rows = self.store.evidence(self.key(fact_id), ["owner"])
        self.assertEqual({row["source_id"] for row in rows}, {message_id, direct_id})
        self.assertEqual(self.store.evidence(self.key(episode_id), ["owner"]), rows)
        self.repo.conn.execute("DELETE FROM memories WHERE id = ?", (episode_id,))
        self.assertEqual(self.store.evidence(self.key(fact_id), ["owner"])[0]["source_id"], direct_id)

    def test_cross_scope_reference_never_uses_other_scope_even_when_authorized(self):
        secret_id = self.message("不可跨域引用的原文", scope="secret")
        episode_id = self.memory("私密摘要", scope="secret", conversation_id="conversation:secret",
                                 meta={"message_range": [secret_id, secret_id]})
        foreign_fact = self.memory("本域存量事实", kind="semantic", meta={"episode_id": episode_id})
        foreign_range = self.memory("本域存量摘要", conversation_id="conversation:secret",
                                    meta={"message_range": [secret_id, secret_id]})
        self.store.sync()
        for memory_id in (foreign_fact, foreign_range):
            rows = self.store.evidence(self.key(memory_id), ["owner", "secret"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_type"], "summary")
            self.assertEqual(rows[0]["source_id"], memory_id)
            self.assertEqual(rows[0]["scope"], "owner")

    def test_cycles_and_malformed_ranges_do_not_create_evidence(self):
        first = self.memory("摘要甲", meta={"message_range": ["bad", {}]})
        second = self.memory("摘要乙", meta={"episode_id": first})
        self.repo.update_memory(first, meta={"episode_id": second, "message_range": [False, 10**100]})
        self.store.sync()
        self.assertEqual(self.store.evidence(self.key(first), ["owner"])[0]["content"], "摘要甲")
        self.assertEqual(self.store.evidence(self.key(second), ["owner"])[0]["content"], "摘要乙")

    def test_range_without_conversation_is_not_guessed(self):
        message_id = self.message("不应被猜测关联的消息")
        memory_id = self.memory("摘要", meta={"message_range": [message_id, message_id]})
        self.store.sync()
        self.assertEqual(self.store.evidence(self.key(memory_id), ["owner"])[0]["source_type"], "summary")

    def test_missing_or_inactive_raw_source_is_explicitly_labelled(self):
        message_id = self.message("[消息已撤回]")
        legacy = self.memory("旧版存量文本", origin="legacy", source_ref="legacy:file:1", subject_id="owner",
                             meta={"message_range": [message_id, message_id]},
                             conversation_id="conversation:owner", end_ts=0)
        summary = self.memory("仅有摘要", meta={"episode_id": "not-found"})
        self.store.sync()
        old = self.store.evidence(self.key(legacy), ["owner"])[0]
        self.assertEqual(old["source_type"], "legacy_memory")
        self.assertEqual(old["content"], "旧版存量文本")
        self.assertEqual(old["source_ref"], "legacy:file:1")
        self.assertEqual(old["ts"], 0)
        self.assertIsNone(old["role"])
        self.assertIsNone(old["actor_id"])
        current = self.store.evidence(self.key(summary), ["owner"])[0]
        self.assertEqual(current["source_type"], "summary")
        self.assertEqual(current["source_ref"], f"memories:{summary}")

    def test_live_legacy_message_is_still_a_message_not_a_summary(self):
        message_id = self.message("旧版真实消息", origin="legacy")
        self.store.sync()
        row = self.store.evidence(self.key(message_id, "message"), ["owner"])[0]
        self.assertEqual(row["source_type"], "message")
        self.assertEqual(row["source_ref"], f"messages:{message_id}")

    def test_fts_repairs_missing_orphan_and_changed_entries(self):
        memory_id = self.memory("应能检索")
        self.store.sync()
        key_id = self.key(memory_id)
        self.store.mark_indexed([key_id])
        self.repo.conn.execute("DELETE FROM retrieval_keys_fts")
        self.repo.conn.execute("INSERT INTO retrieval_keys_fts(rowid, id, tokens) VALUES (999, 'ghost', 'ghost')")
        stats = self.store.sync()
        self.assertEqual(stats["fts_repaired"], 2)
        self.assertEqual(self.fts_ids(), {key_id})
        self.assertEqual(self.store.bm25_query("检索", ["owner"])[0][0], key_id)
        self.repo.conn.execute("UPDATE retrieval_keys_fts SET tokens = 'corrupt'")
        self.assertEqual(self.store.sync()["fts_repaired"], 2)
        self.assertEqual(self.store.unindexed_keys(), [])
        self.assertEqual(self.store.sync()["fts_repaired"], 0)
        self.repo.conn.execute("DROP TABLE retrieval_keys_fts")
        self.assertEqual(self.store.sync()["fts_repaired"], 1)
        self.assertEqual(self.store.bm25_query("检索", ["owner"])[0][0], key_id)

    def test_rebuild_preserves_source_data_and_ids(self):
        self.memory("第一段\n\n第二段", meta={"keywords": ["重建"]})
        self.message("原始消息")
        before = self.source_snapshot()
        self.store.sync()
        original = self.store.get_keys(self.store.all_ids(), ["owner"])
        self.repo.conn.execute("DELETE FROM retrieval_keys")
        self.assertEqual(self.fts_ids(), set())
        self.assertEqual(self.store.sync()["added"], len(original))
        self.assertEqual(self.store.get_keys(self.store.all_ids(), ["owner"]), original)
        self.assertEqual(self.source_snapshot(), before)

    def test_failed_sync_rolls_back_keys_fts_and_marks_inside_caller_transaction(self):
        memory_id = self.memory("旧文本")
        self.store.sync()
        self.store.mark_indexed(self.store.all_ids())
        previous = [dict(row) for row in self.repo.conn.execute("SELECT * FROM retrieval_keys")]
        old_fts = [tuple(row) for row in self.repo.conn.execute("SELECT * FROM retrieval_keys_fts")]
        generate = self.store._source_keys

        def fail_after_one(row, source_type):
            yield next(generate(row, source_type))
            raise RuntimeError("simulated build failure")

        with self.repo.transaction():
            self.repo.update_memory(memory_id, content="新文本")
            with patch.object(self.store, "_source_keys", fail_after_one):
                with self.assertRaisesRegex(RuntimeError, "simulated build failure"):
                    self.store.sync()
            self.assertTrue(self.repo.conn.in_transaction)
            self.assertEqual(self.repo.get_memory(memory_id)["content"], "新文本")
            self.assertEqual([dict(row) for row in self.repo.conn.execute("SELECT * FROM retrieval_keys")], previous)
            self.assertEqual([tuple(row) for row in self.repo.conn.execute("SELECT * FROM retrieval_keys_fts")], old_fts)
        self.assertEqual(self.store.sync()["updated"], 1)

    def test_successful_sync_does_not_commit_callers_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "caller rollback"):
            with self.repo.transaction():
                self.memory("不应提交")
                self.store.sync()
                raise RuntimeError("caller rollback")
        self.assertEqual(self.store.all_ids(), [])
        self.assertEqual(self.fts_ids(), set())
        self.assertEqual(self.repo.count_memories(), 0)

    def test_fts_schema_failure_leaves_sources_and_schema_version_untouched(self):
        repo = MemoryRepository(":memory:")
        self.addCleanup(repo.close)
        repo.add_memory("semantic", "owner", "已有原文")
        schema_before = [tuple(row) for row in repo.conn.execute("SELECT * FROM sqlite_master ORDER BY name")]

        def no_fts(action, arg1, arg2, database, trigger):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_VTABLE else sqlite3.SQLITE_OK

        with repo.transaction():
            repo.add_memory("semantic", "owner", "调用者尚未提交")
            repo.conn.set_authorizer(no_fts)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    EvidenceStore(repo)
            finally:
                repo.conn.set_authorizer(None)
            self.assertTrue(repo.conn.in_transaction)
            self.assertEqual(repo.count_memories(), 2)
            self.assertEqual([tuple(row) for row in repo.conn.execute("SELECT * FROM sqlite_master ORDER BY name")],
                             schema_before)
        self.assertEqual(repo.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "1")
        self.assertEqual(EvidenceStore(repo).sync()["added"], 2)

    def test_large_id_batches_and_empty_mark_operations(self):
        for i in range(410):
            self.memory(f"记录{i}", memory_id=f"batch:{i}")
        self.store.sync()
        ids = self.store.all_ids()
        self.assertEqual(len(self.store.get_keys(ids, ["owner"])), 410)
        self.store.mark_indexed(ids)
        self.store.mark_indexed([])
        self.assertEqual(self.store.unindexed_keys(), [])
        self.store.clear_index_marks()
        self.assertEqual(len(self.store.unindexed_keys(limit=1000)), 410)

    def test_archived_dialogue_is_only_accessible_through_summary_evidence(self):
        message_id = self.message("管理员确认喜欢 Grapefruit Moon", origin="archived")
        memory_id = self.memory("管理员表达了对 Grapefruit Moon 的喜爱",
                                meta={"source_message_ids": [message_id]})
        self.store.sync()
        self.assertFalse(self.store.bm25_query("Grapefruit Moon", ["owner"], kinds=["message"]))
        evidence = self.store.evidence(self.key(memory_id), ["owner"], query="Grapefruit Moon")
        self.assertEqual(evidence[0]["source_id"], message_id)
        self.assertEqual(evidence[0]["content"], "管理员确认喜欢 Grapefruit Moon")
        self.assertFalse(self.store.evidence(self.key(memory_id), ["other"]))


if __name__ == "__main__":
    unittest.main()
