import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from helpers import FakeEmbedder, MemoryFixture
from memory.server import serve
from memory.service import MemoryService
from memory.vector_index import VectorIndex
from memory.vector_maintenance import VectorMaintenance


class SignedEmbedder:
    def __init__(self, sign):
        self.sign = sign
        self.documents = 0

    def embed_documents(self, texts):
        self.documents += len(texts)
        return [[self.sign, 0.0] for _ in texts]

    def embed_query(self, text):
        return [self.sign, 0.0]


class PausedEmbedder(FakeEmbedder):
    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()

    def embed_documents(self, texts):
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test worker was not released")
        return super().embed_documents(texts)


class VectorVersionTests(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()

    def tearDown(self):
        self.fx.close()

    def replace_index(self, embedder):
        index = VectorIndex(self.fx.settings.index_dir, embedder)
        service = MemoryService(self.fx.repo, index, self.fx.settings, self.fx.clock)
        return index, service

    def test_same_dimension_model_switch_rebuilds_and_retires_old_collections(self):
        old, service = self.replace_index(SignedEmbedder(1.0))
        memory = service.add_memory(kind="semantic", scope="owner", content="我喜欢咖啡")
        self.assertTrue(old.query_keys("完全不同措辞", ["owner"]))
        old._client.get_or_create_collection("memories")
        old._client.get_or_create_collection("memory_keys_v2")
        changed, service = self.replace_index(SignedEmbedder(-1.0))
        self.assertNotEqual(old.fingerprint, changed.fingerprint)
        self.assertFalse(changed.status()["ready"])
        self.assertEqual(changed.query_keys("咖啡", ["owner"]), [])
        result = service.reconcile_index()
        self.assertGreater(result["keys"]["vectors"], 0)
        self.assertTrue(changed.status()["ready"])
        self.assertEqual(changed.query("完全不同措辞", ["owner"])[0][0], memory["id"])
        collections = {getattr(row, "name", str(row)) for row in changed._client.list_collections()}
        self.assertEqual(collections, {changed.generation})
        reopened = VectorIndex(self.fx.settings.index_dir, SignedEmbedder(-1.0))
        self.assertEqual(reopened.generation, changed.generation)
        self.assertFalse(reopened.prepare(self.fx.repo))

    def test_single_collection_embeds_each_key_once_and_supports_subject_lookup(self):
        embedder = SignedEmbedder(1.0)
        index, service = self.replace_index(embedder)
        item = service.add_memory(kind="semantic", scope="owner", subject_id="owner", content="喜欢热咖啡")
        service.reconcile_index()
        count = self.fx.repo.conn.execute("SELECT COUNT(*) FROM retrieval_keys").fetchone()[0]
        self.assertEqual(embedder.documents, count)
        self.assertEqual(index.count(), count)
        self.assertEqual(index.query("热饮", ["owner"], subject_id="owner")[0][0], item["id"])
        self.assertEqual(index.query("热饮", ["owner"], subject_id="someone-else"), [])

    def test_lost_staging_collection_invalidates_sql_index_marks(self):
        self.fx.service.add_memory(kind="semantic", scope="owner", content="可重建原文")
        self.fx.index.reset()
        self.fx.index.prepare(self.fx.repo)
        self.fx.repo.conn.execute("UPDATE retrieval_keys SET indexed_at=1")
        generation = self.fx.index.generation
        self.fx.index._client.delete_collection(generation)
        reopened = VectorIndex(self.fx.settings.index_dir, FakeEmbedder())
        self.assertNotEqual(reopened.generation, generation)
        self.assertTrue(reopened.prepare(self.fx.repo))
        self.assertEqual(self.fx.repo.conn.execute(
            "SELECT COUNT(*) FROM retrieval_keys WHERE indexed_at IS NOT NULL").fetchone()[0], 0)

    def test_cleanup_does_not_block_reset_or_remove_active_and_pending(self):
        self.fx.service.add_memory(kind="semantic", scope="owner", content="现有有效索引")
        index = self.fx.index
        index._client.get_or_create_collection("memories")
        active = index.generation
        started, release = threading.Event(), threading.Event()
        original = index._client.delete_collection
        def delayed(name):
            if name == "memories":
                started.set()
                if not release.wait(3):
                    raise TimeoutError("test cleanup not released")
            return original(name)
        with patch.object(index._client, "delete_collection", side_effect=delayed):
            worker = threading.Thread(target=index.retire_obsolete, args=(active,))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                before = time.monotonic()
                index.reset()
                self.assertLess(time.monotonic() - before, .5)
            finally:
                release.set()
                worker.join(3)
        collections = {getattr(row, "name", str(row)) for row in index._client.list_collections()}
        self.assertTrue({active, index.generation}.issubset(collections))


class VectorMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()
        self.fx.service.add_memory(kind="semantic", scope="owner", content="基础记忆")
        self.embedder = PausedEmbedder()
        self.fx.index.embedder = self.embedder
        self.maintenance = VectorMaintenance(self.fx.service, time_budget_ms=100)
        self.fx.repo.vector_maintenance = self.maintenance

    def tearDown(self):
        self.embedder.release.set()
        if self.maintenance._future is not None:
            self.maintenance._future.result(timeout=5)
        self.maintenance.close()
        self.fx.close()

    def start_batch(self):
        self.maintenance.tick()
        self.assertTrue(self.embedder.started.wait(2))

    def drain(self):
        for _ in range(20):
            self.maintenance.tick()
            if self.maintenance._future is not None:
                self.maintenance._future.result(timeout=5)
            elif not self.maintenance.status()["queued"]:
                return
        self.fail("maintenance did not drain")

    def test_maintenance_does_not_block_lexical_search_or_acknowledge_stale_content(self):
        item = self.fx.service.add_memory(kind="semantic", scope="owner", content="北京旧向量")
        self.start_batch()
        started = time.monotonic()
        self.assertEqual(self.fx.index.query_keys("基础记忆", ["owner"]), [])
        self.fx.service.update_memory(item["id"], content="上海新向量")
        results = self.fx.service.search("上海新向量", scopes=["owner"])
        self.assertIn(item["id"], [row["id"] for row in results])
        self.assertLess(time.monotonic() - started, .5)
        self.embedder.release.set()
        self.maintenance._future.result(timeout=5)
        self.maintenance.tick()
        row = self.fx.repo.conn.execute("SELECT indexed_at FROM retrieval_keys WHERE memory_id=?", (item["id"],)).fetchone()
        self.assertIsNone(row[0])
        self.drain()
        self.assertTrue(self.fx.repo.conn.execute("SELECT indexed_at FROM retrieval_keys WHERE memory_id=?", (item["id"],)).fetchone()[0])

    def test_delete_during_embedding_cannot_resurrect_a_vector(self):
        item = self.fx.service.add_memory(kind="semantic", scope="owner", content="待删除信息")
        self.start_batch()
        self.fx.service.delete_memory(item["id"])
        self.embedder.release.set()
        self.drain()
        self.assertFalse(any(key.startswith(f"m:{item['id']}:") for key in self.fx.index.key_ids()))

    def test_rebuild_ignores_previous_generation_completion(self):
        self.fx.service.add_memory(kind="semantic", scope="owner", content="重建中的消息")
        self.start_batch()
        old = self.fx.index.generation
        result = self.fx.service.rebuild_index()
        self.assertTrue(result["queued"])
        self.assertNotEqual(old, self.fx.index.generation)
        self.embedder.release.set()
        self.drain()
        self.assertTrue(self.fx.index.status()["ready"])
        self.assertFalse(self.fx.index.status()["building"])

    def test_rpc_ping_stays_responsive_during_blocked_embedding(self):
        import zmq
        self.fx.service.add_memory(kind="semantic", scope="owner", content="慢速嵌入")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ready, stop = threading.Event(), threading.Event()
        with patch("memory.server.build_service", return_value=self.fx.service), patch.object(self.fx.repo, "close"):
            worker = threading.Thread(target=serve, args=(port, ready.set),
                                      kwargs={"host": "127.0.0.1", "token": "test-token", "stop_event": stop})
            worker.start()
            client = zmq.Context.instance().socket(zmq.REQ)
            client.setsockopt(zmq.RCVTIMEO, 2000)
            client.connect(f"tcp://127.0.0.1:{port}")
            try:
                self.assertTrue(ready.wait(2))
                self.assertTrue(self.embedder.started.wait(2))
                started = time.monotonic()
                client.send_json({"token": "test-token", "method": "ping"})
                self.assertTrue(json.loads(client.recv())["ok"])
                self.assertLess(time.monotonic() - started, .5)
            finally:
                stop.set()
                self.embedder.release.set()
                worker.join(3)
                client.close(linger=0)
            self.assertFalse(worker.is_alive())
