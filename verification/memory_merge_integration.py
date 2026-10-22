"""Merge compatibility checks: real HTTP bridge, database migration and scoped state."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]
from chatBot.gateway import GatewayServer
from chatBot.gateway_client import GatewayClient
from chatBot.memory_hub import GatewayMemoryClient, MemoryHub, identity_envelope
from chatBot.messages import LOCAL_IDENTITY
from chatBot.store import MemoryStore, prepare_gateway_database
from memory.repository import MemoryRepository
from memory.settings import MemorySettings
from tools.context import ToolContext, tool_context
from verification.napcat_integration import FakeAPI, event
from chatBot.capabilities import CapabilityService


class MemoryMergeIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = MemoryStore(self.directory / "gateway.db", owners={"1"})
        self.hub = MemoryHub(self.store, profile="chatbot", settings=MemorySettings(
            data_dir=self.directory, owner_qq_ids=frozenset({"1"}),
            summarize_trigger=4, summarize_chunk=4,
        ))
        self.server = GatewayServer(
            CapabilityService(FakeAPI(), self.store), ("127.0.0.1", 0), "test", self.hub,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.gateway = GatewayClient(f"http://127.0.0.1:{self.server.server_port}", "test")
        self.memory = GatewayMemoryClient(self.gateway)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.hub.close()
        self.temp.cleanup()

    def context(self, ev):
        self.store.ingest(ev)
        with self.store.connection() as db:
            event_id = db.execute("SELECT id FROM events WHERE raw=?", (
                json.dumps(ev, ensure_ascii=False, sort_keys=True),
            )).fetchone()[0]
        return ToolContext(self.store.identity(ev), event_id, "turn")

    def test_owner_continuity_and_account_group_isolation_over_http(self):
        self.memory.record_turn(identity_envelope(LOCAL_IDENTITY), "桌面秘密约定", "我记住了")
        with tool_context(self.context(event("1"))):
            owner = self.memory.build_context({}, query="秘密")
        self.assertIn("桌面秘密约定", str(owner["other_channels"]))
        with tool_context(self.context(event("1", group="10", text="群一"))):
            group = self.memory.build_context({"scope": "owner"}, query="秘密")
        self.assertEqual(group["scope"], "qq:99:group:10")
        self.assertNotIn("桌面秘密约定", str(group))
        with tool_context(self.context(event("2", group="10", self_id=100, text="另一机器人"))):
            other_account = self.memory.build_context({})
        self.assertNotIn("群一", str(other_account))
        with tool_context(self.context(event("2", text="陌生人"))):
            stranger = self.memory.build_context({"channel_type": "local", "scope": "owner"})
            with self.assertRaises(RuntimeError):
                self.memory.export()
        self.assertEqual(stranger["scope"], "qq:99:private:2")
        self.assertNotIn("桌面秘密约定", str(stranger))

    def test_tool_state_never_writes_owner_from_group(self):
        from tools.memory_tool import update_status
        self.memory.put_state("owner", {"important_thing": "主人秘密"})
        group_context = self.context(event("1", group="10"))
        with tool_context(group_context), patch(
            "chatBot.memory_hub.GatewayMemoryClient", return_value=self.memory,
        ):
            result = update_status(add_item="群内物品", important_thing="群内约定")
            self.assertIn("动态状态已更新", result)
            group_state = self.memory.get_state("owner")
            visible = self.memory.build_context({})
            with self.assertRaises(RuntimeError):
                self.memory.put_state("owner", {}, expected_version=0)
        self.assertEqual(group_state["data"]["inventory"], ["群内物品"])
        self.assertIn("群内约定", str(visible["state"]))
        self.assertEqual(self.memory.get_state("owner")["data"], {"important_thing": "主人秘密"})
        with tool_context(self.context(event("2", group="10", msg_id=2))):
            self.assertNotIn("群内物品", str(self.memory.get_state("owner")))

    def test_background_worker_updates_state_through_gateway(self):
        from memory.worker import MemoryWorker
        self.memory.put_state("owner", {"relationship_score": 133})
        ctx = self.context(event("1", text="我信任你 我们周五一起听歌"))
        with tool_context(ctx):
            self.memory.build_context({}, query="听歌")
        worker = MemoryWorker(self.memory, lambda m: "", lambda: "",
            state_complete_text=lambda m: '{"relationship_delta":1,"active_quest":"周五一起听歌"}')
        self.assertTrue(worker.run_once())
        state = self.memory.get_state("owner")["data"]
        self.assertEqual(state["relationship_score"], 134)
        self.assertEqual(state["active_quest"], "周五一起听歌")

    def test_memory_search_ignores_model_supplied_owner_scope_in_group(self):
        self.memory.add_memory("semantic", "owner", "主人生日秘密", subject_id="owner")
        self.memory.add_memory("episode", "qq:99:group:10", "群里的耳机约定")
        with tool_context(self.context(event("1", group="10"))):
            hits = self.memory.search("耳机", ["owner"], k=10)
        self.assertEqual([hit["content"] for hit in hits], ["群里的耳机约定"])
        with tool_context(self.context(event("1", text="生日"))):
            hits = self.memory.search("生日", ["qq:99:group:10"], k=10)
        self.assertEqual([hit["content"] for hit in hits], ["主人生日秘密"])

    def test_manual_state_write_cancels_same_message_background_job(self):
        from tools.memory_tool import update_status
        self.memory.put_state("owner", {"relationship_score": 133})
        ctx = self.context(event("1", text="谢谢你一直陪我"))
        with tool_context(ctx), patch("chatBot.memory_hub.GatewayMemoryClient", return_value=self.memory):
            self.memory.build_context({})
            job = self.hub.service.claim_job(kinds=["update_state"])
            self.assertIsNotNone(job)
            update_status(relationship_delta=1)
        result = self.memory.complete_state_update(job["job"]["id"], {"relationship_delta": 1})
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(self.memory.get_state("owner")["data"]["relationship_score"], 134)

    def test_history_import_does_not_reapply_old_relationship_changes(self):
        ev = event("1", msg_id=456, text="历史问候")
        self.store.add(self.store.identity(ev), "user", "谢谢你", external_id="99:p:1:455")
        ctx = self.context(ev)
        with tool_context(ctx):
            self.memory.build_context({})
        jobs = self.hub.service.repo.conn.execute(
            "SELECT payload FROM jobs WHERE kind='update_state'",
        ).fetchall()
        self.assertEqual(len(jobs), 1)

    def test_sync_crash_replay_and_recall_cancel_stale_job(self):
        contexts = [self.context(event("2", msg_id=i, text=f"消息{i}")) for i in range(1, 5)]
        self.hub.sync()
        scope = "qq:99:private:2"
        self.assertEqual(self.memory.stats()["messages"][scope], 4)
        with self.store.connection() as db:
            db.execute("UPDATE messages SET memory_synced=0")
        self.hub.sync()
        self.assertEqual(self.memory.stats()["messages"][scope], 4)
        claimed = self.memory.claim_job()
        self.assertIsNotNone(claimed)
        self.store.ingest(event("2", msg_id=1, post_type="notice", notice_type="friend_recall"))
        self.hub.sync()
        result = self.memory.complete_summary(claimed["job"]["id"], "包含撤回内容", [], [], 5)
        self.assertEqual(result["status"], "ignored")
        with tool_context(contexts[-1]):
            self.assertIn("[消息已撤回]", str(self.memory.build_context({})["working"]))

    def test_legacy_state_and_memories_import_once(self):
        self.store.update_state(LOCAL_IDENTITY, {"important_thing": "旧约定"})
        with self.store.connection() as db:
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?)", (
                "old-summary", "owner", "chat_memory", "旧情节", '{"type":"episode_summary"}', 1700000000,
            ))
        self.assertEqual(self.hub.import_legacy()["imported"], 2)
        self.assertEqual(self.hub.import_legacy()["imported"], 0)
        self.assertEqual(self.memory.get_state("owner")["data"]["important_thing"], "旧约定")
        self.assertEqual(self.memory.list_memories(kinds=["episode"])["total"], 1)
        self.assertEqual(self.memory.stats()["messages"], {})

    def test_lexical_mode_retains_chroma_reconciliation_marks(self):
        service = self.hub.service
        old_id = service.repo.add_memory("semantic", "owner", "已有向量")
        service.repo.mark_indexed([old_id])
        service.delete_memory(old_id)
        new_id = service.add_memory("semantic", "owner", "等待向量")["id"]
        service.rebuild_index()
        self.assertIn(old_id, service.repo.deleted_indexed_memory_ids())
        self.assertIn(new_id, [row["id"] for row in service.repo.unindexed_memories()])

    def test_full_gateway_proxies_to_real_zmq_memory_server(self):
        sys.path.insert(0, str(ROOT / "thingking" / "tests"))
        from test_end_to_end import InProcessServer
        from memory.client import MemoryClient
        remote = InProcessServer(self.hub.service)
        proxy = MemoryHub(
            self.store, profile="full", client=MemoryClient("127.0.0.1", remote.port),
        )
        try:
            self.server.memory_hub = proxy
            with tool_context(self.context(event("2", text="完整模式消息"))):
                context = self.memory.build_context({})
                self.memory.put_state("owner", {"important_thing": "用户状态"})
            self.assertEqual(context["scope"], "qq:99:private:2")
            self.assertIn("完整模式消息", str(context["working"]))
            self.assertEqual(self.memory.get_state("qq:99:private:2")["data"]["important_thing"], "用户状态")
            self.assertEqual(self.memory.get_state("owner")["data"], {})
        finally:
            self.server.memory_hub = self.hub
            proxy.close()
            remote.close()

    def test_previous_database_moved_and_new_schema_preserved(self):
        directory = self.directory / "migration"
        old = MemoryStore(directory / "memory.db", owners={"1"})
        old.ingest(event("1", text="迁移原消息"))
        original = old.context(old.identity(event("1")))
        destination = prepare_gateway_database(directory)
        self.assertEqual(destination, directory / "gateway.db")
        moved = MemoryStore(destination, owners={"1"})
        self.assertEqual(moved.context(moved.identity(event("1"))), original)
        repo = MemoryRepository(directory / "memory.db")
        repo.put_state("owner", {"important_thing": "新版数据"})
        self.assertEqual(prepare_gateway_database(directory), destination)
        self.assertEqual(repo.get_state("owner")["data"], {"important_thing": "新版数据"})
        repo.close()

    def test_profile_dependencies_keep_three_python_processes(self):
        from process_registry import get_processes
        light = get_processes(profile="chatbot")
        full = {row["name"]: row for row in get_processes(profile="full")}
        self.assertEqual([row["name"] for row in light], ["ChatBot", "QQ Thinking", "Memory Worker"])
        self.assertEqual(light[0]["depends_on"], [])
        self.assertEqual(full["Memory Worker"]["depends_on"], ["ChatBot"])
        for name in ("ChatBot", "Thinking", "RAG Web"):
            self.assertIn("RAG Server", full[name]["depends_on"])


if __name__ == "__main__":
    unittest.main()
