import os
import threading
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helpers import FakeEmbedder, MemoryFixture
from memory.chunking import EmbeddingChunks
from memory.service import MemoryService
from memory.vector_index import VectorIndex
from memory.vector_maintenance import VectorMaintenance


class RecordingEmbedder(FakeEmbedder):
    def __init__(self):
        self.documents = []
        self.poison = True

    def embed_documents(self, texts):
        self.documents.extend(texts)
        if self.poison and any("POISON" in text for text in texts):
            raise ValueError("invalid fixture embedding")
        return super().embed_documents(texts)


class VectorConstructionTests(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()
        self.embedder = RecordingEmbedder()
        self.fx.index = VectorIndex(self.fx.settings.index_dir, self.embedder)
        self.fx.service = MemoryService(self.fx.repo, self.fx.index, self.fx.settings, self.fx.clock)
        self.maintenance = None

    def tearDown(self):
        if self.maintenance:
            if self.maintenance._future:
                self.maintenance._future.result(timeout=5)
            self.maintenance.close()
        self.fx.close()

    def schedule(self, size=1):
        self.maintenance = VectorMaintenance(self.fx.service, batch_size=size, time_budget_ms=100)
        self.fx.repo.vector_maintenance = self.maintenance
        return self.maintenance

    def step(self):
        self.maintenance.tick()
        if self.maintenance._future:
            self.maintenance._future.result(timeout=5)

    def drain(self):
        for _ in range(40):
            self.step()
            if not self.maintenance.status()["queued"]:
                return
        self.fail("vector maintenance did not drain")

    def test_poison_first_key_does_not_starve_healthy_key_and_survives_restart(self):
        maintenance = self.schedule()
        self.fx.service.add_memory(kind="episode", scope="owner", memory_id="aaa", content="POISON 坏片段")
        good = self.fx.service.add_memory(kind="episode", scope="owner", memory_id="zzz", content="健康片段 HEALTHY")
        self.drain()
        self.assertTrue(maintenance.status()["ready"])
        self.assertEqual(maintenance.status()["quarantined_keys"], 1)
        self.assertIn(good["id"], [mid for mid, _ in self.fx.index.query("HEALTHY", ["owner"])])
        calls = len(self.embedder.documents)
        maintenance.close()
        self.schedule()
        self.drain()
        self.assertEqual(len(self.embedder.documents), calls)
        self.assertEqual(self.maintenance.status()["quarantined_keys"], 1)
        self.embedder.poison = False
        self.fx.clock.advance(3601)
        self.drain()
        self.assertEqual(self.maintenance.status()["quarantined_keys"], 0)
        self.assertEqual(self.maintenance.status()["pending_keys"], 0)

    def test_mixed_batch_bisects_and_changed_poison_key_can_retry_immediately(self):
        self.schedule(8)
        bad = self.fx.service.add_memory(kind="episode", scope="owner", memory_id="a", content="POISON invalid")
        good = self.fx.service.add_memory(kind="episode", scope="owner", memory_id="b", content="正常数据")
        self.drain()
        self.assertIn(good["id"], [mid for mid, _ in self.fx.index.query("正常", ["owner"])])
        self.fx.service.update_memory(bad["id"], content="修复后的数据")
        self.drain()
        self.assertEqual(self.maintenance.status()["pending_keys"], 0)
        self.assertFalse(self.maintenance.status()["degraded"])

    def test_only_changed_chunk_embeds_and_rebuild_reuses_persistent_cache(self):
        text = "".join(f"片段{i:03d} " + "甲乙丙丁" * 40 + "。\n" for i in range(24)) + "尾巴A"
        item = self.fx.service.add_memory(kind="episode", scope="owner", content=text)
        before = {row[0] for row in self.fx.repo.conn.execute("SELECT search_text FROM retrieval_keys")}
        self.assertGreater(len(before), 3)
        self.embedder.documents.clear()
        self.fx.service.update_memory(item["id"], content=text[:-1] + "B")
        after = {row[0] for row in self.fx.repo.conn.execute("SELECT search_text FROM retrieval_keys")}
        self.assertEqual(len(after - before), 1)
        self.assertEqual(self.embedder.documents, list(after - before))
        reopened = RecordingEmbedder()
        index = VectorIndex(self.fx.settings.index_dir, reopened)
        service = MemoryService(self.fx.repo, index, self.fx.settings, self.fx.clock)
        index.reset()
        service.reconcile_index()
        self.assertEqual(reopened.documents, [])
        self.assertTrue(index.status()["ready"])

    def test_cached_query_survives_busy_embedder_and_uncached_has_explicit_busy_trace(self):
        self.fx.service.add_memory(kind="episode", scope="owner", content="查询缓存证据")
        before = self.fx.index.query_keys("查询缓存", ["owner"])
        self.assertTrue(before)
        with self.fx.index._embedding_lock:
            self.assertEqual(self.fx.index.query_keys("查询缓存", ["owner"]), before)
            self.assertTrue(self.fx.index.last_query_trace["cache_hit"])
            self.assertEqual(self.fx.index.query_keys("从未查询过的词", ["owner"]), [])
            self.assertEqual(self.fx.index.last_query_trace["status"], "busy")

    def test_invalid_query_embedding_is_not_cached_and_provider_can_recover(self):
        self.fx.service.add_memory(kind="episode", scope="owner", content="健康查询来源")
        for label, invalid in (("dimensions", [1.0]), ("nonfinite", [float("nan")] * self.embedder.dim)):
            with self.subTest(label=label):
                query = "recover-" + label
                valid = FakeEmbedder.embed_query(self.embedder, query)
                with patch.object(self.embedder, "embed_query", side_effect=[invalid, valid]) as provider:
                    with self.assertRaisesRegex(ValueError, "query embedding"):
                        self.fx.index.query_keys(query, ["owner"])
                    self.assertTrue(self.fx.index.query_keys(query, ["owner"]))
                    self.assertTrue(self.fx.index.query_keys(query, ["owner"]))
                    self.assertEqual(provider.call_count, 2)
                    self.assertTrue(self.fx.index.last_query_trace["cache_hit"])

    def test_foreground_query_runs_between_background_microbatches(self):
        self.fx.service.add_memory(kind="episode", scope="owner", content="已有向量")
        started, release = threading.Event(), threading.Event()
        events = []
        original = self.embedder.embed_documents
        def documents(texts):
            events.append("documents")
            if len(events) == 1:
                started.set()
                release.wait(3)
            return original(texts)
        def query(text):
            events.append("query")
            return FakeEmbedder.embed_query(self.embedder, text)
        keys = [{"id": f"test-{i}", "search_text": f"新片段{i}", "scope": "owner", "kind": "episode",
                 "source_hash": str(i), "meta": {}} for i in range(8)]
        with patch.object(self.embedder, "embed_documents", side_effect=documents), patch.object(self.embedder, "embed_query", side_effect=query):
            worker = threading.Thread(target=self.fx.index.upsert_keys, args=(keys,))
            foreground = threading.Thread(target=self.fx.index.query_keys, args=("优先查询", ["owner"]))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                foreground.start()
                deadline = time.monotonic() + .05
                while not self.fx.index._query_waiters and time.monotonic() < deadline:
                    time.sleep(.001)
                release.set()
                foreground.join(2)
                worker.join(3)
                self.assertEqual(events[:3], ["documents", "query", "documents"])
            finally:
                release.set()
                worker.join(3)
                if foreground.ident:
                    foreground.join(3)

    def test_continuous_ingestion_does_not_prevent_build_cutover(self):
        self.fx.service.add_memory(kind="episode", scope="owner", memory_id="zzz", content="构建开始前的记录")
        self.schedule()
        self.maintenance.rebuild()
        for number in range(10):
            self.fx.service.add_memory(kind="episode", scope="owner", memory_id=f"a{number:03d}", content=f"不断到达的数据{number}")
            self.step()
            if self.fx.index.status()["ready"] and not self.fx.index.status()["building"]:
                break
        self.assertFalse(self.fx.index.status()["building"])
        self.assertGreater(self.maintenance.status()["pending_keys"], 0)
        self.drain()
        self.assertEqual(self.maintenance.status()["pending_keys"], 0)

    def test_continuously_rewritten_source_cannot_block_healthy_initial_snapshot(self):
        self.schedule(2)
        stable = self.fx.service.add_memory(kind="episode", scope="owner", content="稳定的健康来源")
        changing = self.fx.service.add_memory(kind="episode", scope="owner", content="持续改写的来源0")
        for number in range(10):
            self.step()
            # Change the source after embedding but before its acknowledgement.
            self.fx.service.update_memory(changing["id"], content=f"持续改写的来源{number+1}")
            if not self.fx.index.status()["building"]:
                break
        self.assertTrue(self.fx.index.status()["ready"])
        self.assertFalse(self.fx.index.status()["building"])
        self.assertIn(stable["id"], [mid for mid, _ in self.fx.index.query("健康来源", ["owner"])])
        self.drain()
        self.assertEqual(self.maintenance.status()["pending_keys"], 0)

    def test_build_member_snapshot_copies_bounded_identity_pages_and_excludes_later_keys(self):
        self.schedule()
        for number in range(270):
            self.fx.repo.add_memory("episode", "owner", f"初始资料{number}")
        keys = self.fx.service.retriever.keys
        keys.sync()
        index = self.fx.index
        index.prepare(self.fx.repo)
        first = index._snapshot_build_members(self.fx.repo)
        self.assertFalse(first["snapshot_complete"])
        self.assertEqual(self.fx.repo.conn.execute("SELECT COUNT(*) FROM retrieval_vector_build_members").fetchone()[0], 128)
        late = self.fx.repo.add_memory("episode", "owner", "快照开始之后的新资料")
        keys.sync()
        second = index._snapshot_build_members(self.fx.repo)
        self.assertFalse(second["snapshot_complete"])
        final = index._snapshot_build_members(self.fx.repo)
        self.assertTrue(final["snapshot_complete"])
        self.assertEqual(self.fx.repo.conn.execute("SELECT COUNT(*) FROM retrieval_vector_build_members").fetchone()[0], 270)
        self.assertFalse(self.fx.repo.conn.execute("SELECT 1 FROM retrieval_vector_build_members WHERE key_id=?", (f"m:{late}:0",)).fetchone())

    def test_manifest_publication_failure_preserves_pending_state_for_retry(self):
        self.schedule(2)
        self.fx.service.add_memory(kind="episode", scope="owner", content="待发布的健康记忆")
        self.step()
        with patch.object(self.fx.index, "_save_manifest", side_effect=OSError("temporary disk error")):
            with self.assertLogs("bines.memory.vector_maintenance", level="ERROR"):
                self.maintenance.tick()
        self.assertTrue(self.fx.index.status()["building"])
        self.assertFalse(self.fx.index.status()["ready"])
        self.maintenance._retry_at = 0
        self.drain()
        self.assertTrue(self.fx.index.query_keys("健康记忆", ["owner"]))

    def test_failed_reset_preserves_active_generation_and_can_be_retried(self):
        self.fx.service.add_memory(kind="episode", scope="owner", content="仍需提供服务的来源")
        self.schedule()
        generation = self.fx.index.generation
        with patch.object(self.fx.index, "_save_manifest", side_effect=OSError("temporary disk error")):
            with self.assertRaises(OSError):
                self.maintenance.rebuild()
        self.assertEqual(self.fx.index.generation, generation)
        self.assertTrue(self.fx.index.status()["ready"])
        self.assertTrue(self.maintenance.status()["queued"])
        self.assertTrue(self.fx.index.query_keys("提供服务", ["owner"]))
        self.maintenance.rebuild()
        self.drain()
        self.assertNotEqual(self.fx.index.generation, generation)
        self.assertTrue(self.fx.index.status()["ready"])

    def test_search_parameter_changes_in_place_and_structural_changes_reuse_embeddings(self):
        self.fx.service.add_memory(kind="episode", scope="owner", content="索引配置测试")
        generation = self.fx.index.generation
        with patch.dict(os.environ, {"BINES_MEMORY_HNSW_EF_SEARCH": "256"}):
            same = VectorIndex(self.fx.settings.index_dir, RecordingEmbedder())
        self.assertEqual(same.generation, generation)
        self.assertEqual(same._keys.configuration["hnsw"]["ef_search"], 256)
        model = RecordingEmbedder()
        with patch.dict(os.environ, {"BINES_MEMORY_HNSW_MAX_NEIGHBORS": "32"}):
            rebuilt = VectorIndex(self.fx.settings.index_dir, model)
        self.assertNotEqual(rebuilt.generation, generation)
        MemoryService(self.fx.repo, rebuilt, self.fx.settings, self.fx.clock).reconcile_index()
        self.assertEqual(model.documents, [])
        self.assertTrue(rebuilt.status()["ready"])

    def test_broken_derived_cache_does_not_quarantine_healthy_sources(self):
        with sqlite3.connect(self.fx.settings.index_dir / "embedding-cache.sqlite3") as conn:
            conn.execute("DROP TABLE vectors")
        self.schedule()
        self.fx.service.add_memory(kind="episode", scope="owner", content="缓存坏了但源数据正常")
        self.drain()
        self.assertEqual(self.maintenance.status()["pending_keys"], 0)
        self.assertEqual(self.maintenance.status()["quarantined_keys"], 0)
        self.assertFalse(self.fx.index.status()["embedding_cache_available"])
        self.assertTrue(self.fx.index.query_keys("源数据", ["owner"]))


class TokenChunkTests(unittest.TestCase):
    def chunks(self, backend, maximum):
        tokenizer = SimpleNamespace(backend_tokenizer=backend, num_special_tokens_to_add=lambda pair: 0)
        return EmbeddingChunks(SimpleNamespace(tokenizer=tokenizer, max_seq_length=maximum))

    def test_byte_tokenizer_never_splits_a_unicode_character_or_exceeds_budget(self):
        from tokenizers import Tokenizer, models, pre_tokenizers
        vocab = {char: i for i, char in enumerate(pre_tokenizers.ByteLevel.alphabet())}
        backend = Tokenizer(models.BPE(vocab, []))
        backend.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
        chunks = self.chunks(backend, 10)
        value = "🙂🙂🙂"
        self.assertLessEqual(len(backend.encode(chunks.fit(value)).ids), 10)
        parts = chunks.split(value)
        self.assertTrue(all(len(backend.encode(part).ids) <= chunks.body_limit for part in parts))
        self.assertTrue(all(part in value for part in parts))
        self.assertTrue(all("�" not in part for part in parts))

    def test_zero_overlap_preserves_whitespace_and_sentence_boundaries(self):
        from tokenizers import Tokenizer, models, pre_tokenizers
        backend = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
        backend.pre_tokenizer = pre_tokenizers.Whitespace()
        chunks = self.chunks(backend, 9)
        text = "a b c d e f g     h i j"
        self.assertEqual("".join(chunks.split(text)), text)

    def test_long_whitespace_windows_are_valid_and_do_not_drop_source_text(self):
        from tokenizers import Tokenizer, models, pre_tokenizers
        backend = Tokenizer(models.BPE({char: i for i, char in enumerate(pre_tokenizers.ByteLevel.alphabet())}, []))
        backend.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
        chunks = self.chunks(backend, 256)
        text = "alpha" + " " * 512 + "omega"
        parts = chunks.split(text)
        self.assertTrue(any(part and not part.strip() for part in parts))
        self.assertEqual("".join(parts).replace(" ", ""), "alphaomega")
        self.assertGreaterEqual(sum(part.count(" ") for part in parts), 512)
        self.assertTrue(all(part in text and len(backend.encode(part).ids) <= chunks.body_limit for part in parts))

    def test_context_reservation_preserves_the_full_body_inside_embedding_limit(self):
        from tokenizers import Tokenizer, models, pre_tokenizers
        backend = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
        backend.pre_tokenizer = pre_tokenizers.Whitespace()
        chunks = self.chunks(backend, 256)
        source = " ".join(f"word{i}" for i in range(600))
        for part in chunks.split(source):
            combined = chunks.fit(part + " " + "context " * 1000)
            self.assertTrue(combined.startswith(part))
            self.assertLessEqual(len(backend.encode(combined).ids), 256)
