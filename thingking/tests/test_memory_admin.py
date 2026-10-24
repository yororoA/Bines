"""Admin views, isolation and backfill scheduling with an isolated database."""
import importlib
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helpers import Clock, local_env
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from memory.server import dispatch
from memory.worker import MemoryWorker


class MemoryAdminTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(1_792_800_000)
        self.repo = MemoryRepository(":memory:", self.clock)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
                                     MemorySettings(state_updates=False, retention_enabled=True,
                                                    summarize_trigger=8, summarize_chunk=8), self.clock)

    def tearDown(self):
        self.repo.close()

    def drain(self):
        while job := self.service.claim_job(["associate"]):
            self.service.complete_association(job["job"]["id"], [], .6)

    def test_detail_preserves_verbatim_raw_and_filters_scope(self):
        result = self.service.record_turn(local_env(self.clock() - 86400), '原话 <script>"引用"</script>', "已收到")
        node = self.service.graph.for_message(result["message_ids"][0])
        detail = self.service.memory_detail(node_id=node["id"], scope="owner")
        self.assertIn('原话 <script>"引用"</script>', detail["content"])
        self.assertEqual(len(detail["roots"]), 2)
        self.assertIsNone(self.service.memory_detail(node_id=node["id"], scope="qq_group:other"))
        self.assertEqual(detail["jobs"][0]["kind"], "associate")

    def test_deleted_source_is_not_exposed(self):
        mid = self.service.add_memory("semantic", "owner", "需要移除的事实")["id"]
        self.service.delete_memory(mid)
        self.assertIsNone(self.service.memory_detail(memory_id=mid))
        self.assertEqual(self.service.list_association_nodes()["total"], 0)

    def test_old_memory_detail_does_not_require_or_mutate_backfill(self):
        mid = self.repo.add_memory("semantic", "owner", "尚未回建的旧记忆")
        before = self.repo.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        detail = self.service.memory_detail(memory_id=mid)
        self.assertEqual(detail["content"], "尚未回建的旧记忆")
        self.assertFalse(detail["registered"])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], before)

    def test_nodes_filters_and_pagination(self):
        self.service.add_memory("semantic", "owner", "蓝色围巾")
        self.service.add_memory("episode", "qq_group:one", "另一条围巾记忆")
        data = self.service.list_association_nodes(scopes=["owner"], keyword="围巾", source_type="semantic")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["scope"], "owner")
        self.assertEqual(self.service.list_association_nodes(ready=1)["total"], 0)
        self.drain()
        self.assertEqual(self.service.list_association_nodes(ready=1)["total"], 2)
        self.assertEqual(self.service.list_association_nodes(offset=1, limit=1)["total"], 2)
        self.assertEqual(len(self.service.list_association_nodes(offset=1, limit=1)["items"]), 1)

    def test_detail_shows_frozen_top3_and_cold_record(self):
        nodes = []
        for i in range(4):
            turn = self.service.record_turn(local_env(self.clock() - 86400), f"围巾约定{i}", "好的")
            nodes.append(self.service.graph.for_message(turn["message_ids"][0]))
        self.drain()
        summary = self.service.claim_job(["summarize"])
        self.service.complete_summary(summary["job"]["id"], "四次约定均已记录")
        details = [self.service.memory_detail(node_id=n["id"]) for n in nodes]
        self.assertEqual(sum(d["node"]["retrieval_status"] == "hot" for d in details), 3)
        self.assertTrue(all(d["snapshots"][0]["applied"] for d in details))
        self.assertTrue(all(d["content"] for d in details))

    def test_unrelated_backfill_does_not_block_new_summary(self):
        old = self.repo.add_memory("semantic", "owner", "很早的另一条记忆")
        self.service.graph.register_memory(old, background=True)
        for i in range(4):
            self.service.record_turn(local_env(self.clock() - 86400), f"新对话{i}", "好的")
        for _ in range(4):
            job = self.service.claim_job(["associate"])
            self.assertFalse(job["job"]["payload"]["background"])
            self.service.complete_association(job["job"]["id"], [], .6)
        self.assertIsNotNone(self.service.claim_job(["summarize"]))

    def test_backfill_has_lower_priority_than_ready_summary(self):
        with self.repo.transaction():
            self.repo.enqueue_job("associate", "owner", {"background": True}, "old")
            self.repo.enqueue_job("summarize", "owner", {}, "summary")
            self.repo.enqueue_job("associate", "owner", {"background": False}, "new")
        self.assertEqual(self.repo.claim_job(300)["dedupe_key"], "new")
        self.assertEqual(self.repo.claim_job(300)["dedupe_key"], "summary")
        self.assertEqual(self.repo.claim_job(300)["dedupe_key"], "old")

    def test_qq_gateway_rejects_all_admin_methods(self):
        from chatBot.memory_hub import MemoryHub
        hub = MemoryHub.__new__(MemoryHub)
        hub.lock = threading.RLock()
        hub._call = lambda *a, **k: self.fail("QQ admin request reached the memory service")
        for method in ("list_association_nodes", "memory_detail", "list_jobs"):
            for owner in (False, True):
                identity = SimpleNamespace(local=False, owner=owner, group=None)
                with self.assertRaises(PermissionError):
                    hub.call(identity, method, {"scope": "owner"})

    def test_http_detail_not_found_and_invalid_page(self):
        with patch("memory.client.MemoryClient.ping", return_value=True):
            web = importlib.import_module("thingking.rag_web_server")
        class Client:
            def __getattr__(_, method):
                def call(**params):
                    result = dispatch(self.service, {"method": method, "params": params})
                    if not result["ok"]:
                        raise ValueError(result["error"])
                    return result["result"]
                return call
        with patch.object(web, "client", Client()):
            http = web.app.test_client()
            self.assertEqual(http.get("/api/detail?node_id=missing").status_code, 404)
            self.assertEqual(http.get("/api/nodes?offset=bad").status_code, 400)
            self.assertEqual(http.get("/api/nodes").status_code, 200)
            self.assertEqual(http.get("/missing").status_code, 404)

    def test_model_repairs_quote_before_association_is_written(self):
        source = self.service.add_memory("semantic", "owner", "用户约好周末送蓝色围巾")["id"]
        target = self.service.add_memory("semantic", "owner", "周末准备把蓝色围巾送给朋友")["id"]
        claimed = self.service.claim_job(["associate"])
        other = self.service.graph.for_memory(target)
        self.assertEqual(claimed["job"]["payload"]["node_id"], self.service.graph.for_memory(source)["id"])
        link = {"target_id": other["id"], "reason_key": "scarf-gift", "reason": "周末赠送蓝色围巾的约定",
                "relation_type": "event", "confidence": .8, "evidence": [
                    {"node_id": claimed["materials"]["source"]["id"], "quote": "错误改写了原话"},
                    {"node_id": other["id"], "quote": "周末准备把蓝色围巾送给朋友"}]}
        calls = []
        def model(messages):
            calls.append(messages[-1]["content"])
            if len(calls) == 2:
                link["evidence"][0]["quote"] = "用户约好周末送蓝色围巾"
            return json.dumps({"prior": .7, "links": [link]}, ensure_ascii=False)
        worker = MemoryWorker(self.service, model, lambda: "")
        worker._associate(claimed["job"], claimed["materials"])
        detail = self.service.memory_detail(memory_id=source)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(detail["edges"]), 1)
        self.assertEqual(detail["edges"][0]["evidence"][0]["quote"], "用户约好周末送蓝色围巾")
        self.assertTrue(detail["memberships"][0]["recall_strength"] > .7)


if __name__ == "__main__":
    unittest.main()
