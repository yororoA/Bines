import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import GROUP_ID, OWNER_QQ, STRANGER_QQ, FakeEmbedder, MemoryFixture

from memory import migration
from memory.models import OWNER_SCOPE

TS = 1_771_514_887.0


def _write_legacy(directory: Path) -> None:
    (directory / "memory_permanent.json").write_text(json.dumps({
        "user_profile": {"name": "YororoIce", "role": "Admin", "preferences": "喜欢甜食, 收集棉花娃娃"},
        "core_rules": ["persona rules now live in persona.yaml"],
        "facts": ["型号: BICE"],
    }, ensure_ascii=False), encoding="utf-8")
    (directory / "memory_dynamic.json").write_text(json.dumps({
        "current_time": "2026-10-01T16:19:02+08:00", "relationship_score": 133, "relationship_level": "非常亲密",
        "memory_highlights": ["【身份契约】2026年1月15日诞生"],
    }, ensure_ascii=False), encoding="utf-8")
    (directory / "memory_short.json").write_text(json.dumps([[
        {"role": "user", "content": "[2026/10/02 00:47]: [晚上好]", "timestamp": TS},
        {"role": "assistant", "content": "已经是凌晨了哦。", "timestamp": TS},
    ]], ensure_ascii=False), encoding="utf-8")
    (directory / "qq_msg_private_buffer.json").write_text(json.dumps({"messages": [
        {"content": "[QQ私聊][嘎 (管理员)]: 你知道我在哪吗", "meta": {"timestamp": TS + 10, "sender": "User", "group_id": None, "user_id": int(OWNER_QQ)}},
        {"content": "[QQ私聊回复][Bot]: 在电脑前吧", "meta": {"timestamp": TS + 11, "sender": "Self(Bot)", "group_id": None, "user_id": int(OWNER_QQ)}},
        {"content": "[QQ私聊][路人]: 你好", "meta": {"timestamp": TS + 12, "sender": "User", "group_id": None, "user_id": int(STRANGER_QQ)}},
    ]}, ensure_ascii=False), encoding="utf-8")
    (directory / "qq_msg_buffer.json").write_text(json.dumps({"messages": [
        {"content": "群里随便聊聊", "meta": {"timestamp": TS + 20, "sender": "小明", "group_id": int(GROUP_ID), "user_id": int(STRANGER_QQ)}},
    ]}, ensure_ascii=False), encoding="utf-8")

    import chromadb

    client = chromadb.PersistentClient(path=str(directory / migration.LEGACY_CHROMA_DIR))
    emb = FakeEmbedder()

    def add(name, docs):
        coll = client.get_or_create_collection(name, embedding_function=None)
        coll.add(ids=[d[0] for d in docs], documents=[d[1] for d in docs], metadatas=[d[2] for d in docs],
                 embeddings=emb.embed_documents([d[1] for d in docs]))

    add("chat_memory", [
        ("c1", "[剧情摘要]管理员和碧波一起听歌", {"type": "episode_summary", "timestamp": TS - 86400, "day_key": "2026-02-18", "importance_score": 7}),
        ("c2", "User: 我对花生过敏\nAssistant: 记住了", {"type": "raw_conversation", "timestamp": TS - 86400, "day_key": "2026-02-18", "importance_score": 9}),
    ])
    add("long_term_diary", [("d1", "[日记 2026-02-18]\n今天很开心", {"type": "diary", "timestamp": TS, "day_key": "2026-02-18"})])
    add("summary_buffer", [("s1", f"【QQ会话摘要 - group_{GROUP_ID}】\n群友在聊游戏", {"type": "summary_buffer", "timestamp": TS, "day_key": "2026-02-19", "tags": "Game,Daily"})])
    add("qq_history_store", [
        ("q1", "群里随便聊聊", {"type": "qq_log", "timestamp": TS + 20, "sender": "小明", "group_id": GROUP_ID, "user_id": STRANGER_QQ}),
        ("q2", "另一条群消息", {"type": "qq_log", "timestamp": TS + 30, "sender": "小红", "group_id": GROUP_ID, "user_id": "40004"}),
    ])


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = Path(tempfile.mkdtemp(prefix="bines_legacy_"))
        _write_legacy(cls.legacy)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.legacy, ignore_errors=True)

    def setUp(self):
        self.fx = MemoryFixture()
        self.snapshot = migration.snapshot_legacy(self.legacy, self.fx.settings.backup_dir)
        self.migrator = migration.LegacyMigrator(self.fx.repo, {OWNER_QQ})
        self.report = self.migrator.run(self.snapshot)

    def tearDown(self):
        self.fx.close()

    def test_snapshot_is_complete_and_verifiable(self):
        self.assertEqual(migration.verify_snapshot(self.snapshot), [])
        manifest = json.loads((self.snapshot / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("memory_short.json", manifest)
        self.assertTrue(any(name.endswith("chroma.sqlite3") for name in manifest))

    def test_rerun_is_idempotent(self):
        before = self.fx.repo.stats()
        again = migration.LegacyMigrator(self.fx.repo, {OWNER_QQ}).run(self.snapshot)
        self.assertEqual(again.imported, {})
        self.assertEqual(self.fx.repo.stats(), before)

    def test_verification_passes(self):
        result = migration.verify_migration(self.fx.repo, self.report.source_counts)
        self.assertEqual(result["problems"], [])

    def test_owner_data_lands_in_owner_scope(self):
        state = self.fx.repo.get_state(OWNER_SCOPE)["data"]
        self.assertEqual(state["relationship_score"], 133)
        self.assertNotIn("current_time", state)
        facts = {m["content"] for m in self.fx.repo.list_memories(kinds=["semantic"], scopes=[OWNER_SCOPE])}
        self.assertIn("管理员的名字/昵称：YororoIce", facts)
        self.assertIn("关于管理员：收集棉花娃娃", facts)
        self.assertFalse(any("persona" in f or "BICE" in f for f in facts))
        kinds = {m["kind"] for m in self.fx.repo.list_memories(scopes=[OWNER_SCOPE])}
        self.assertEqual(kinds, {"semantic", "episode", "excerpt", "diary"})

    def test_short_term_becomes_pending_local_messages(self):
        local = self.fx.repo.recent_messages("local", 10)
        self.assertEqual([(m["role"], m["content"]) for m in local], [("user", "晚上好"), ("assistant", "已经是凌晨了哦。")])
        self.assertEqual(self.fx.repo.pending_message_count("local"), 2)

    def test_qq_messages_are_scoped_and_deduplicated(self):
        owner_private = self.fx.repo.recent_messages(f"qq_private:{OWNER_QQ}", 10)
        self.assertEqual([(m["scope"], m["role"]) for m in owner_private], [(OWNER_SCOPE, "user"), (OWNER_SCOPE, "assistant")])
        stranger = self.fx.repo.recent_messages(f"qq_private:{STRANGER_QQ}", 10)
        self.assertEqual([(m["scope"], m["content"]) for m in stranger], [(f"qq_private:{STRANGER_QQ}", "你好")])
        group = self.fx.repo.recent_messages(f"qq_group:{GROUP_ID}", 10)
        self.assertEqual([m["content"] for m in group], ["群里随便聊聊", "另一条群消息"])
        self.assertTrue(all(m["role"] == "observed" and m["episode_id"] == "legacy" for m in group))

    def test_group_summary_buffer_goes_to_group_scope(self):
        group_eps = self.fx.repo.list_memories(kinds=["episode"], scopes=[f"qq_group:{GROUP_ID}"])
        self.assertEqual(len(group_eps), 1)
        self.assertEqual(group_eps[0]["tags"], ["Game", "Daily"])

    def test_migrated_memories_are_recallable_after_indexing(self):
        self.fx.service.reconcile_index()
        hits = self.fx.service.search("管理员和碧波一起听歌", [OWNER_SCOPE], k=1)
        self.assertEqual(hits[0]["content"], "[剧情摘要]管理员和碧波一起听歌")
        group_hits = self.fx.service.search("群友在聊游戏", [OWNER_SCOPE], k=5)
        self.assertNotIn("群友在聊游戏", "".join(h["content"] for h in group_hits))

    def test_legacy_episodes_can_fill_missing_historical_diaries(self):
        self.fx.clock.advance(400 * 86400)
        self.assertEqual(self.fx.service.enqueue_due_diaries(), 1)
        claimed = self.fx.service.claim_job(kinds=["diary"])
        self.assertEqual(claimed["job"]["kind"], "diary")
        self.assertNotEqual(claimed["materials"]["day_key"], self.fx.service._day(self.fx.clock()))
        self.assertEqual(claimed["materials"]["state"], {})


if __name__ == "__main__":
    unittest.main()
