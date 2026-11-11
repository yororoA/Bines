"""Cold retrieval contracts: temporary SQLite/Chroma, deterministic embeddings, no API."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]


class ConceptEmbedder:
    """Controlled synonyms test the real Chroma protocol, not model quality."""
    def __init__(self):
        self.queries = []

    @staticmethod
    def vector(text):
        if "围巾" in text or "保暖纪念品" in text:
            return [1., 0., 0.]
        if "QZ981" in text or "预约代号" in text:
            return [0., 1., 0.]
        return [0., 0., 1.]

    def embed_query(self, text):
        self.queries.append(text)
        return self.vector(text)

    def embed_documents(self, texts):
        return [self.vector(text) for text in texts]


class ArchiveRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bines-archive-retrieval-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(root),
            "BINES_GATEWAY_DIR": str(root / "gateway"), "BINES_HEALTH_DIR": str(root / "health"),
            "BINES_MODEL_USAGE_PATH": str(root / "usage.db"), "BINES_WEBSITE_BRIDGE_SPOOL": str(root / "website"),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "ANONYMIZED_TELEMETRY": "False",
        })
        env.start()
        self.addCleanup(env.stop)
        from memory.repository import MemoryRepository
        from memory.service import MemoryService
        from memory.settings import MemorySettings
        from memory.vector_index import VectorIndex
        self.now = 1_792_800_000.
        self.repo = MemoryRepository(root / "memory.db", clock=lambda: self.now)
        self.addCleanup(self.repo.close)
        self.embedder = ConceptEmbedder()
        self.index = VectorIndex(root / "index", self.embedder)
        self.service = MemoryService(self.repo, self.index, MemorySettings(
            data_dir=root, state_updates=False, retention_enabled=True,
            associations_enabled=True, cold_fallback_limit=3), clock=lambda: self.now)

    def cold(self, kind="episode", text="围巾收在银色柜子里。", scope="owner", **meta):
        if kind == "episode":
            mid = self.repo.add_memory(kind, scope, text, meta=meta)
            node = self.service.graph.register_memory(mid)
            parent = self.repo.add_memory("diary", scope, "当天处理了一些事情。", meta={"episode_ids": [mid]})
            self.service.graph.register_memory(parent)
            self.repo.conn.execute("UPDATE association_nodes SET retrieval_status='cold',activation_factor=.1,"
                                   "consolidated_into=? WHERE id=?", (parent, node["id"]))
        else:
            self.repo.ensure_conversation("archive:" + scope, "local", scope)
            mid = self.repo.add_message("archive:" + scope, scope, "user", text, self.now, "2026-10-24",
                                        actor_id="owner", origin="archived")
            parent = self.repo.add_memory("episode", scope, "当天处理了一些事情。", meta={"source_message_ids": [mid]})
            self.repo.conn.execute("UPDATE messages SET episode_id=? WHERE id=?", (parent, mid))
            node = self.service.graph.register_raw([self.repo.get_message(mid)])
            self.service.graph.register_memory(parent)
        return mid, parent, node

    def index_all(self):
        return self.service.retriever.sync(vectors=True, full=True)

    def node_ids(self, query, scopes=None, **kwargs):
        return {row["node_id"] for row in self.service.search(query, scopes or ["owner"], **kwargs)}

    def test_exact_episode_and_message_rescue_without_association_edges(self):
        for kind in ("episode", "message"):
            with self.subTest(kind=kind):
                _, _, node = self.cold(kind, "预约代号 QZ981")
                for query in ("QZ981", "之前我提到的预约代号QZ981是什么"):
                    self.assertIn(node["id"], self.node_ids(query))
                self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM association_edges").fetchone()[0], 0)

    def test_precise_cold_match_competes_when_loose_hot_candidates_fill_quota(self):
        _, _, node = self.cold("message", "玻璃风铃已经挂在东窗。")
        for index in range(12):
            self.service.graph.register_memory(self.repo.add_memory("episode", "owner", f"玻璃厂家样品 {index}"))
        self.assertIn(node["id"], self.node_ids("玻璃风铃", k=3))

    def url_archive(self, kind, distractors, scope):
        # Synthetic URL only. Shared tokens/numbers give the shorter near-misses
        # higher BM25 scores, while the complete quoted fragment occurs once.
        phrase = "https://archive.example.invalid/records/20261021/image?token=123456&size=800&view=preview"
        near = phrase.replace("?", "/").replace("=", "/").replace("&", "/")
        for _ in range(distractors):
            self.cold(kind, near, scope)
        mid, _, node = self.cold(kind, phrase + "。" + "无关的背景记录。" * 45, scope)
        self.service.retriever.sync()
        lexical = self.service.retriever.keys.bm25_query(
            phrase, [scope], [kind], 200, cold_only=True, per_source=1, scan_limit=256)
        prefix = f"m:{mid}:" if kind == "episode" else f"u:{mid}:"
        rank = next(position for position, (kid, _) in enumerate(lexical, 1) if kid.startswith(prefix))
        return phrase, node, rank

    def test_quoted_url_rescues_message_and_episode_beyond_first_twelve_sources(self):
        from memory.vector_index import VectorMatches
        for kind in ("message", "episode"):
            with self.subTest(kind=kind):
                scope = "url-archive:" + kind
                phrase, node, rank = self.url_archive(kind, 33, scope)
                self.assertGreater(rank, 12)
                self.assertLessEqual(rank, 128)
                with patch.object(self.index, "query_keys", return_value=VectorMatches()), \
                        patch.object(self.index, "query_archive_keys", return_value=VectorMatches()):
                    self.assertIn(node["id"], self.node_ids(f'原话“{phrase}”', [scope], k=1))
                self.assertLessEqual(self.service.retriever.last_trace["cold_fallback"], 3)

    def test_quoted_fragment_cannot_be_replaced_by_its_numeric_substrings(self):
        _, _, node = self.cold("message", "记录日期20261021，编号123456。")
        self.index_all()
        # Even an eligible vector hit must preserve the explicit quote. Plain
        # numeric lookup remains supported when no quoted fragment was given.
        self.assertNotIn(node["id"], self.node_ids('原文“20261021/image?token=123456”'))
        self.assertIn(node["id"], self.node_ids("123456"))

    def test_quoted_match_in_later_chunk_is_checked_before_source_deduplication(self):
        from memory.vector_index import VectorMatches
        phrase = "https://archive.example.invalid/image?token=123456&size=800"
        near = phrase.replace("?", "/").replace("=", "/").replace("&", "/")
        _, _, node = self.cold("episode", (near + "\n") * 8 + "\n" + phrase + "。" + "背景资料。" * 45)
        self.service.retriever.sync()
        lexical = self.service.retriever.keys.bm25_query(
            phrase, ["owner"], ["episode"], 128, cold_only=True, scan_limit=128)
        rows = self.service.retriever.keys.get_keys([kid for kid, _ in lexical], ["owner"], include_cold=True)
        self.assertNotIn(phrase, rows[lexical[0][0]]["content"])
        self.assertTrue(any(phrase in row["content"] for row in rows.values()))
        with patch.object(self.index, "query_keys", return_value=VectorMatches()), \
                patch.object(self.index, "query_archive_keys", return_value=VectorMatches()):
            self.assertIn(node["id"], self.node_ids(f'原话“{phrase}”', k=1))

    def test_quoted_archive_scan_remains_bounded_at_128_keys(self):
        from memory.vector_index import VectorMatches
        scope = "url-archive:bounded"
        phrase, node, rank = self.url_archive("message", 136, scope)
        self.assertGreater(rank, 128)
        with patch.object(self.index, "query_keys", return_value=VectorMatches()), \
                patch.object(self.index, "query_archive_keys", return_value=VectorMatches()), \
                patch.object(self.service.retriever.keys, "bm25_query",
                             wraps=self.service.retriever.keys.bm25_query) as lexical:
            self.assertNotIn(node["id"], self.node_ids(f'原话“{phrase}”', [scope]))
        cold_calls = [call for call in lexical.call_args_list if call.kwargs.get("cold_only")]
        self.assertEqual(len(cold_calls), 1)
        self.assertEqual(cold_calls[0].kwargs["scan_limit"], 128)
        self.assertLessEqual(self.service.retriever.last_trace["cold_fallback"], 3)

    def test_synonym_query_reaches_first_and_tail_chunks_and_reuses_query_embedding(self):
        for kind, text in (("message", "围巾收在银色柜子里。"),
                           ("episode", "普通流水记录。" * 300 + "围巾收在银色柜子里。")):
            with self.subTest(kind=kind):
                _, _, node = self.cold(kind, text)
                self.index_all()
                query = "保暖纪念品的位置"
                before = self.embedder.queries.count(query)
                hits = self.service.search(query, ["owner"])
                match = next(row for row in hits if row["node_id"] == node["id"])
                self.assertTrue(any("围巾" in span for span in match["matched_spans"]))
                self.assertEqual(self.embedder.queries.count(query), max(1, before))
                self.assertGreater(self.service.retriever.last_trace["cold_fallback"], 0)

    def test_cold_vectors_are_bounded_but_all_original_chunks_remain_lexical(self):
        mid, _, _ = self.cold(text="普通流水记录。" * 600 + "围巾收在银色柜子里。")
        self.index_all()
        rows = self.repo.conn.execute("SELECT id,meta FROM retrieval_keys WHERE memory_id=? ORDER BY chunk_index", (mid,)).fetchall()
        self.assertGreater(len(rows), 3)
        selected = [row["id"] for row in rows if json.loads(row["meta"]).get("archive_vector")]
        self.assertEqual(selected, [rows[0]["id"], rows[-1]["id"]])
        self.assertEqual(set(selected), set(self.index.key_ids()).intersection(row["id"] for row in rows))
        self.assertEqual(self.service.retriever.keys.vector_pending_count(), 0)

    def test_scope_time_and_withdrawal_guard_both_archive_channels(self):
        for kind in ("episode", "message"):
            with self.subTest(kind=kind):
                mid, _, node = self.cold(kind)
                self.index_all()
                self.assertNotIn(node["id"], self.node_ids("保暖纪念品的位置", ["qq:other:private:7"]))
                self.assertNotIn(node["id"], self.node_ids("保暖纪念品的位置", as_of=self.now - 1))
                if kind == "episode":
                    self.repo.temporal.retract_source("owner", mid)
                else:
                    self.repo.conn.execute("UPDATE messages SET content='[消息已撤回]' WHERE id=?", (mid,))
                self.assertNotIn(node["id"], self.node_ids("保暖纪念品的位置", include_history=True))
                self.assertNotIn(node["id"], self.node_ids("围巾"))

    def test_deleted_audit_repair_and_invalid_parent_cannot_return_through_stale_vectors(self):
        for flag in ("deleted", "audit_only", "repair_pending", "parent_deleted"):
            with self.subTest(flag=flag):
                mid, parent, node = self.cold("message" if flag == "parent_deleted" else "episode")
                self.index_all()
                if flag in {"deleted", "parent_deleted"}:
                    self.repo.delete_memory(parent if flag == "parent_deleted" else mid)
                else:
                    self.repo.update_memory(mid, meta={flag: True})
                self.assertNotIn(node["id"], self.node_ids("保暖纪念品的位置"))
                self.assertNotIn(node["id"], self.node_ids("围巾"))

    def test_corrected_archived_original_keeps_correction_in_results(self):
        _, parent, node = self.cold("message", "围巾收在北京的柜子里。")
        self.index_all()
        self.service.correct_memory(scope="owner", node_id=node["id"], operation_id="fix-city",
            expected_digest=node["digest"], kind="fact", quote="北京", replacement="上海", explanation="用户纠正地点")
        # Until repair completes, the parent remains invalid and the original
        # cannot bypass it through either archive channel.
        self.assertNotIn(node["id"], self.node_ids("保暖纪念品的位置"))
        self.assertTrue(self.repo.get_memory(parent)["meta"].get("repair_pending"))
        claimed = self.service.claim_job(["repair_memory"])
        self.assertIsNotNone(claimed)
        self.service.complete_memory_repair(claimed["job"]["id"], "围巾收在上海的柜子里。")
        self.index_all()
        hits = self.service.search("保暖纪念品的位置", ["owner"])
        relevant = [hit for hit in hits if "围巾" in hit["content"]]
        self.assertTrue(relevant)
        self.assertTrue(all(any(correction["replacement"] == "上海" for correction in hit["corrections"])
                            for hit in relevant))

    def test_withdrawn_raw_dependency_invalidates_cold_episode(self):
        mid, _, _ = self.cold("message")
        _, _, episode = self.cold("episode", source_message_ids=[mid])
        self.index_all()
        self.repo.conn.execute("UPDATE messages SET content='[消息已撤回]' WHERE id=?", (mid,))
        self.assertNotIn(episode["id"], self.node_ids("保暖纪念品的位置"))
        self.assertNotIn(episode["id"], self.node_ids("围巾"))

    def test_archive_quota_counts_independent_sources_not_vector_chunks(self):
        for _ in range(7):
            self.cold(text="围巾" + "普通流水记录。" * 250 + "围巾放在柜子里。")
        self.index_all()
        hits = self.service.search("保暖纪念品的位置", ["owner"], k=10)
        cold = [row for row in hits if row["retrieval_status"] == "cold"]
        self.assertEqual(len(cold), 3)
        self.assertEqual(len({row["node_id"] for row in cold}), 3)

    def test_partial_literal_corroboration_requires_an_eligible_semantic_hit(self):
        from memory.vector_index import VectorMatches
        distractors = [self.cold(text="围巾在商场陈列。")[0] for _ in range(4)]
        target_id, _, target = self.cold(text="你送来的围巾收在柜子里。")
        self.index_all()
        pairs = [(f"m:{mid}:0", .1) for mid in distractors] + [(f"m:{target_id}:0", .45)]
        keys = self.service.retriever.keys.get_keys([kid for kid, _ in pairs], ["owner"], include_cold=True)
        result = VectorMatches(pairs, {kid: key["source_hash"] for kid, key in keys.items()},
                               {kid: key["search_hash"] for kid, key in keys.items()})
        query = "你送的保暖纪念品在哪里"
        with patch.object(self.index, "query_archive_keys", return_value=result):
            self.assertIn(target["id"], self.node_ids(query, k=3))
        with patch.object(self.index, "query_archive_keys", return_value=VectorMatches()):
            self.assertNotIn(target["id"], self.node_ids(query, k=3))

    def test_archive_vectors_cannot_starve_hot_candidates_including_legacy_metadata(self):
        self.service.retriever.settings = replace(self.service.retriever.settings, retrieval_candidates=10)
        self.embedder.vector = lambda text: ([1., .2, 0.] if "热记忆" in text else
                                             [1., 0., 0.] if "围巾" in text or "保暖纪念品" in text else [0., 0., 1.])
        hot_id = self.repo.add_memory("episode", "owner", "热记忆：围巾收在房间的盒子里。")
        hot = self.service.graph.register_memory(hot_id)
        self.index_all()
        query = "保暖纪念品的位置"
        self.assertIn(hot["id"], self.node_ids(query))
        key_id = f"m:{hot_id}:0"
        # Old on-disk vectors have no tier metadata. They remain valid while
        # archive representatives are incrementally added to this collection.
        saved = self.index._keys.get(ids=[key_id], include=["embeddings", "documents", "metadatas"])
        metadata = dict(saved["metadatas"][0])
        metadata.pop("retrieval_status")
        self.index._keys.delete(ids=[key_id])
        self.index._keys.upsert(ids=[key_id], embeddings=saved["embeddings"],
                                documents=saved["documents"], metadatas=[metadata])
        for number in range(260):
            self.cold(text=f"围巾归档记录 {number}。")
        self.index_all()
        self.assertIn(hot["id"], self.node_ids(query, k=10))
        self.assertEqual(self.service.retriever.last_trace["vector_requested"], 10)
        self.assertNotIn(key_id, {kid for kid, _ in self.index.query_archive_keys(query, ["owner"], n=128)})

    def test_old_signature_requeues_cold_sources_and_async_backfill_finishes(self):
        from memory.evidence import EvidenceStore
        from memory.vector_maintenance import VectorMaintenance
        mid, _, _ = self.cold(text="普通流水记录。" * 300 + "围巾放在柜子里。")
        self.service.retriever.sync()
        self.repo.conn.execute("DELETE FROM retrieval_keys WHERE memory_id=?", (mid,))
        self.repo.conn.execute("DELETE FROM retrieval_dirty_sources")
        self.repo.conn.execute("UPDATE retrieval_sync_state SET value='chars-700-80:cold-fts-v2:search-hash-v1' "
                               "WHERE key='chunking_signature'")
        EvidenceStore(self.repo, index=self.index)
        self.assertGreater(self.repo.conn.execute("SELECT COUNT(*) FROM retrieval_dirty_sources").fetchone()[0], 0)
        maintenance = VectorMaintenance(self.service, batch_size=2, time_budget_ms=1000)
        self.addCleanup(maintenance.close)
        for _ in range(100):
            maintenance.tick()
            if maintenance._future:
                maintenance._future.result(timeout=10)
            if not maintenance.status()["queued"]:
                break
        self.assertEqual(maintenance.status()["pending_keys"], 0)
        self.assertFalse(maintenance.status()["queued"])
        self.assertEqual(len([key for key in self.index.key_ids() if key.startswith(f"m:{mid}:")]), 2)


if __name__ == "__main__":
    unittest.main()
