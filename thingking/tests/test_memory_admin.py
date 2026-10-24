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
        for method in ("list_association_nodes", "memory_detail", "memory_graph", "list_jobs"):
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
            self.assertEqual(http.get("/api/graph").status_code, 200)
            self.assertEqual(http.get("/api/graph").headers["Cache-Control"], "no-store")
            self.assertEqual(http.get("/constellation").status_code, 200)
            self.assertEqual(http.get("/constellation_worker.js").status_code, 200)
            self.assertEqual(http.get("/missing").status_code, 404)

    def test_graph_includes_all_unindexed_sources_without_scheduling_jobs(self):
        for i in range(151):
            self.repo.add_memory("semantic", "owner", f"历史记忆{i}")
        self.repo.ensure_conversation("old", "local", "owner")
        message = self.repo.add_message("old", "owner", "user", "未回建原文",
                                       self.clock(), "2026-10-23", actor_id="owner")
        before = self.repo.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        view = self.service.memory_graph()
        self.assertEqual(view["counts"]["nodes"], 152)
        self.assertEqual(view["counts"]["unregistered"], 152)
        self.assertEqual(view["edges"], [])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], before)
        detail = self.service.memory_detail(message_id=message)
        self.assertEqual(detail["content"], "未回建原文")
        self.assertIsNone(self.service.memory_detail(message_id=message, scope="qq_group:other"))

    def test_graph_keeps_long_source_searchable_and_uses_scoped_detail_identifiers(self):
        text = "较长的历史材料。" * 60 + "尾部约定：周六归还围巾"
        mid = self.repo.add_memory("semantic", "owner", text)
        node = self.service.memory_graph()["nodes"][0]
        self.assertLessEqual(len(node["preview"]), 280)
        self.assertIn("周六归还围巾", node["search_text"])
        self.assertEqual(node["detail_params"], {"memory_id": mid})
        self.assertEqual(self.service.memory_detail(**node["detail_params"], scope="owner")["content"], text)

    def test_graph_scope_and_complete_raw_turns(self):
        result = self.service.record_turn(local_env(self.clock() - 86400), "真实原话", "回应")
        self.service.add_memory("semantic", "qq_group:other", "另一个作用域")
        view = self.service.memory_graph(["owner"])
        self.assertEqual(view["counts"]["raw"], 1)
        self.assertEqual(view["nodes"][0]["message_ids"], result["message_ids"])
        self.assertEqual({n["scope"] for n in view["nodes"]}, {"owner"})

    def test_graph_does_not_expose_forgotten_sources_or_stale_edges(self):
        source = self.service.add_memory("semantic", "owner", "需要修正的围巾约定")["id"]
        target = self.service.add_memory("semantic", "owner", "另一份围巾约定")["id"]
        claimed = self.service.claim_job(["associate"])
        a, b = claimed["materials"]["source"], self.service.graph.for_memory(target)
        link = {"target_id": b["id"], "reason_key": "scarf", "reason": "围巾约定", "relation_type": "event",
                "confidence": .8, "evidence": [{"node_id": a["id"], "quote": a["content"]},
                                             {"node_id": b["id"], "quote": "另一份围巾约定"}]}
        self.service.complete_association(claimed["job"]["id"], [link])
        self.assertEqual(len(self.service.memory_graph()["edges"]), 1)
        self.repo.update_memory(source, content="已经修订的原文")
        view = self.service.memory_graph()
        self.assertEqual(view["edges"], [])
        self.assertTrue(any(n["preview"] == "已经修订的原文" for n in view["nodes"]))
        self.service.delete_memory(target)
        self.assertFalse(any(n["memory_id"] == target for n in self.service.memory_graph()["nodes"]))

    def test_graph_brightness_is_maximum_of_both_chain_weights(self):
        source = self.service.add_memory("semantic", "owner", "围巾约定一")["id"]
        target = self.service.add_memory("semantic", "owner", "围巾约定二")["id"]
        claimed = self.service.claim_job(["associate"])
        a, b = claimed["materials"]["source"], self.service.graph.for_memory(target)
        link = {"target_id": b["id"], "reason_key": "scarf", "reason": "围巾约定", "relation_type": "support",
                "confidence": .8, "evidence": [{"node_id": a["id"], "quote": a["content"]},
                                             {"node_id": b["id"], "quote": "围巾约定二"}]}
        self.service.complete_association(claimed["job"]["id"], [link])
        self.repo.conn.execute("""UPDATE chain_memberships SET recall_strength=.83,citation_strength=.92,
            scored_revision=(SELECT revision FROM association_chains WHERE id=chain_memberships.chain_id)""")
        node = next(n for n in self.service.memory_graph()["nodes"] if n["memory_id"] == source)
        self.assertEqual(node["max_weight"], .92)
        self.assertEqual(node["max_recall"], .83)
        self.assertEqual(node["weight_basis"], "chain_max")

    def test_graph_shows_audit_records_without_making_them_retrievable(self):
        mid = self.repo.add_memory("episode", "owner", "只读审计记录", meta={"audit_only": True})
        view = self.service.memory_graph()
        self.assertEqual(len(view["nodes"]), 1)
        self.assertTrue(view["nodes"][0]["record_only"])
        self.assertEqual(self.service.memory_detail(memory_id=mid)["content"], "只读审计记录")
        self.assertEqual(self.service.search("只读审计记录", ["owner"]), [])

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
