"""Recovery exercises real SQLite copies, source collisions and evidence links."""
import copy
import hashlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memory.evidence import EvidenceStore
from memory.history_merge import apply_plan, fingerprint, make_plan, readonly, stage_plan
from memory.models import content_hash
from memory.repository import MemoryRepository


class HistoryMergeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / "source.db", self.root / "target.db"
        with self.repo(self.source) as repo:
            repo.upsert_identity("owner", "owner")
            repo.link_identity("qq", "7", "owner")
            repo.ensure_conversation("qq_private:7", "qq_private", "owner")
            repo.ensure_conversation("qq_group:9", "qq_group", "qq_group:9")
            self.msg = repo.add_message("qq_group:9", "qq_group:9", "user", "我喜欢青柠汽水", 10, "2026-01-01", actor_id="qq:8")
            self.extra = repo.add_message("qq_group:9", "qq_group:9", "assistant", "记住青柠汽水啦", 11, "2026-01-01", actor_id="assistant")
            self.owner = repo.add_message("qq_private:7", "owner", "user", "别把群聊当作我的事实", 12, "2026-01-01", actor_id="owner")
            self.episode = repo.add_memory("episode", "qq_group:9", "群友谈青柠汽水", conversation_id="qq_group:9", day_key="2026-01-01", meta={"message_range": [self.msg, self.extra]})
            self.fact = repo.add_memory("semantic", "qq_group:9", "喜欢青柠汽水", subject_id="qq:8", conversation_id="qq_group:9", meta={"episode_id": self.episode})
            self.unproven = repo.add_memory("semantic", "qq_group:9", "对方每周都去咖啡馆", subject_id="qq:8", conversation_id="qq_group:9", meta={"episode_id": self.episode})
            repo.conn.execute("UPDATE messages SET episode_id=? WHERE id IN (?,?)", (self.episode, self.msg, self.extra))
            self.candidates = {"messages": [{"id": self.msg, "hash": content_hash("我喜欢青柠汽水"), "scope_type": "qq_group"},
                                             {"id": self.owner, "hash": content_hash("别把群聊当作我的事实"), "scope_type": "owner"}],
                               "memories": [{"id": m, "hash": content_hash(repo.get_memory(m)["content"]), "scope_type": "qq_group"}
                                            for m in (self.episode, self.fact, self.unproven)]}
        with self.repo(self.target) as repo:
            repo.upsert_identity("owner", "owner")
            repo.link_identity("qq", "7", "owner")
            repo.ensure_conversation("qq_private:7", "qq_private", "owner")
            repo.ensure_conversation("local", "local", "owner")
            for n in range(4):
                repo.add_message("local", "owner", "user", "目标原有内容" + str(n), n, "2026-02-01", actor_id="owner")
            repo.put_state("owner", {"relationship_score": 42})
        self.manifest = self.seal()

    from contextlib import contextmanager

    @contextmanager
    def repo(self, path):
        repo = MemoryRepository(path)
        try:
            yield repo
        finally:
            repo.close()

    def seal(self):
        return {"files": [{"path": str(p), "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in (self.source, Path(str(self.source) + "-wal"), Path(str(self.source) + "-shm")) if p.exists()]}

    def plan(self):
        return make_plan(self.source, self.target, self.manifest, self.candidates)

    def test_copy_recovery_remaps_ids_preserves_scope_and_is_idempotent(self):
        with readonly(self.target) as conn:
            original = fingerprint(conn)
        plan = self.plan()
        self.assertEqual(plan["summary"]["messages_insert"], 3)
        self.assertEqual(plan["summary"]["dependency_messages_added"], 1)
        self.assertEqual(plan["summary"]["memories_quarantined"], 1)
        stage = self.root / "staged.db"
        result = stage_plan(plan, stage)
        self.assertEqual(result["messages_inserted"], 3)
        self.assertTrue(all(i > 4 for i in result["message_ids"]))
        with readonly(self.target) as conn:
            self.assertEqual(fingerprint(conn), original)
        with self.repo(stage) as repo:
            mapping = result["memory_mapping"]
            episode = repo.get_memory(mapping[self.episode])
            self.assertNotIn("message_range", episode["meta"])
            self.assertEqual(set(episode["meta"]["source_message_ids"]), {result["message_mapping"][str(self.msg)], result["message_mapping"][str(self.extra)]})
            self.assertFalse(repo.get_memory(mapping[self.fact])["deleted"])
            self.assertTrue(repo.get_memory(mapping[self.unproven])["deleted"])
            self.assertEqual(repo.get_memory(mapping[self.fact])["meta"]["episode_id"], mapping[self.episode])
            self.assertEqual(repo.get_state("owner")["data"]["relationship_score"], 42)
            self.assertEqual(repo.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(repo.conn.execute("SELECT count(*) FROM messages WHERE origin='archived'").fetchone()[0], 3)
            self.assertEqual(repo.conn.execute("SELECT count(*) FROM source_dependencies WHERE memory_id=?", (mapping[self.episode],)).fetchone()[0], 2)
            store = EvidenceStore(repo)
            store.sync()
            keys = repo.conn.execute("SELECT scope FROM retrieval_keys WHERE memory_id=?", (mapping[self.episode],)).fetchall()
            self.assertTrue(keys)
            self.assertEqual({r[0] for r in keys}, {"qq_group:9"})
            self.assertEqual(repo.conn.execute("SELECT count(*) FROM retrieval_keys WHERE memory_id=?", (mapping[self.unproven],)).fetchone()[0], 0)
            self.assertTrue(repo.conn.execute("SELECT id FROM retrieval_keys_fts WHERE tokens MATCH '青柠'").fetchall())
        again = apply_plan(plan, stage)
        self.assertEqual(again["messages_inserted"], 0)
        self.assertEqual(again["memories_inserted"], 0)

    def test_hash_target_and_plan_changes_fail_without_writing(self):
        plan = self.plan()
        corrupt = copy.deepcopy(plan)
        corrupt["messages"][0]["scope"] = "owner"
        with self.assertRaisesRegex(ValueError, "plan was changed"):
            apply_plan(corrupt)
        with self.repo(self.target) as repo:
            repo.put_state("owner", {"relationship_score": 43})
        with self.assertRaisesRegex(ValueError, "baseline changed"):
            apply_plan(plan)
        with self.repo(self.source) as repo:
            repo.conn.execute("UPDATE messages SET content='改过的来源' WHERE id=?", (self.msg,))
        with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
            self.plan()

    def test_changed_owner_link_or_cross_scope_evidence_rejected(self):
        with self.repo(self.target) as repo:
            repo.upsert_identity("qq:7", "qq")
            repo.link_identity("qq", "7", "qq:7")
        with self.assertRaisesRegex(ValueError, "independently linked"):
            self.plan()
        with self.repo(self.target) as repo:
            repo.link_identity("qq", "7", "owner")
        with self.repo(self.source) as repo:
            repo.update_memory(self.episode, meta={"source_message_ids": [self.owner]})
        self.manifest = self.seal()
        with self.assertRaisesRegex(ValueError, "cross-scope"):
            self.plan()

    def test_same_text_different_scope_actor_or_time_is_not_deduplicated(self):
        with self.repo(self.target) as repo:
            repo.ensure_conversation("qq_group:9", "qq_group", "qq_group:9")
            repo.add_message("local", "owner", "user", "我喜欢青柠汽水", 10, "2026-01-01", actor_id="owner")
            repo.add_message("qq_group:9", "qq_group:9", "user", "我喜欢青柠汽水", 10, "2026-01-01", actor_id="qq:other")
            repo.add_message("qq_group:9", "qq_group:9", "user", "我喜欢青柠汽水", 99, "2026-01-01", actor_id="qq:8")
        self.assertEqual(self.plan()["summary"]["messages_insert"], 3)
        with self.repo(self.target) as repo:
            same = repo.add_message("qq_group:9", "qq_group:9", "user", "我喜欢青柠汽水", 10, "2026-01-01", actor_id="qq:8")
        plan = self.plan()
        item = next(x for x in plan["messages"] if x["source_id"] == str(self.msg))
        self.assertEqual(item["target_id"], str(same))
        self.assertEqual(plan["summary"]["messages_insert"], 2)

    def test_duplicate_source_turns_share_new_id_and_provenance_tampering_is_rejected(self):
        with self.repo(self.source) as repo:
            duplicate = repo.add_message("qq_group:9", "qq_group:9", "user", "我喜欢青柠汽水", 10, "2026-01-01", actor_id="qq:8")
        self.candidates["messages"].append({"id": duplicate, "hash": content_hash("我喜欢青柠汽水"), "scope_type": "qq_group"})
        self.manifest = self.seal()
        plan = self.plan()
        self.assertEqual(plan["summary"]["messages_insert"], 3)
        staged = self.root / "staged.db"
        result = stage_plan(plan, staged)
        self.assertEqual(result["message_mapping"][str(duplicate)], result["message_mapping"][str(self.msg)])
        self.assertTrue(apply_plan(plan, staged)["already_applied"])
        with self.repo(staged) as repo:
            memory = repo.get_memory(result["memory_mapping"][self.episode])
            repo.update_memory(memory["id"], meta={**memory["meta"], "source_message_ids": [1]})
        with self.assertRaisesRegex(ValueError, "provenance or dependencies changed"):
            apply_plan(plan, staged)

    def test_transfer_refuses_replacement_before_any_docker_or_archive_action(self):
        path = Path(__file__).resolve().parents[2] / ".tools" / "memory_transfer.py"
        # tests/ -> thingking/ -> repo root
        spec = importlib.util.spec_from_file_location("history_transfer_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        directory = self.root / "existing-memory"
        directory.mkdir()
        (directory / "memory.db").write_bytes(b"untouched")
        with patch.object(module, "MEMORY_DIR", directory), patch.object(module, "do_verify") as verify, \
                patch.object(module, "running_containers") as docker, redirect_stdout(io.StringIO()):
            self.assertEqual(module.do_import(self.root / "archive.tar.gz"), 2)
            verify.assert_not_called()
            docker.assert_not_called()
        self.assertEqual((directory / "memory.db").read_bytes(), b"untouched")

    def test_withdrawn_message_and_erased_source_reference_block_derived_restore(self):
        with self.repo(self.target) as repo:
            repo.ensure_conversation("qq_group:9", "qq_group", "qq_group:9")
            withdrawn = repo.add_message("qq_group:9", "qq_group:9", "user", "  我喜欢青柠汽水  ", 10,
                                         "2026-01-01", actor_id="qq:8", origin="withdrawn")
        with self.assertRaisesRegex(ValueError, "deleted or withdrawn"):
            self.plan()
        with self.repo(self.source) as repo:
            repo.conn.execute("UPDATE messages SET source_ref='wire:old-message' WHERE id=?", (self.msg,))
        self.manifest = self.seal()
        with self.repo(self.target) as repo:
            repo.conn.execute("UPDATE messages SET content='[消息已撤回]',source_ref='wire:old-message' WHERE id=?", (withdrawn,))
        with self.assertRaisesRegex(ValueError, "deleted or withdrawn"):
            self.plan()
        with readonly(self.target) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM memories").fetchone()[0], 0)

    def test_deleted_episode_blocks_restore_even_when_original_messages_are_missing(self):
        with self.repo(self.target) as repo:
            repo.ensure_conversation("qq_group:9", "qq_group", "qq_group:9")
            old = repo.add_memory("episode", "qq_group:9", "群友谈青柠汽水", conversation_id="qq_group:9", day_key="2026-01-01")
            repo.delete_memory(old)
        with self.assertRaisesRegex(ValueError, "deleted or withdrawn"):
            self.plan()

    def test_cli_rejects_all_output_aliases_before_any_database_write(self):
        script = Path(__file__).resolve().parents[2] / "thingking/scripts/merge_memory_history.py"
        manifest, candidates, plan_path = (self.root / name for name in ("manifest.json", "candidates.json", "plan.json"))
        manifest.write_text(json.dumps(self.manifest))
        candidates.write_text(json.dumps(self.candidates))
        plan_path.write_text(json.dumps(self.plan()))
        alias = self.root / "alias.db"
        alias.symlink_to(self.target)
        hardlink = self.root / "hardlink.db"
        hardlink.hardlink_to(self.target)
        before = self.target.read_bytes()
        outputs = [self.source, self.target, manifest, candidates, alias, hardlink, Path(str(self.target) + "-journal")]
        for output in outputs:
            with self.subTest(output=output.name):
                call = subprocess.run([sys.executable, str(script), "plan", "--source", str(self.source),
                                       "--target", str(self.target), "--manifest", str(manifest), "--candidates", str(candidates),
                                       "--output", str(output)], capture_output=True, text=True)
                self.assertEqual(call.returncode, 2)
                self.assertIn("output collides", call.stderr)
                self.assertEqual(self.target.read_bytes(), before)
        stage = self.root / "should-not-exist.db"
        for command in (["stage", "--output", str(stage)], ["apply", "--target", str(self.target)]):
            call = subprocess.run([sys.executable, str(script), *command, "--plan", str(plan_path),
                                   "--report", str(self.target)], capture_output=True, text=True)
            self.assertEqual(call.returncode, 2)
            self.assertFalse(stage.exists())
            self.assertEqual(self.target.read_bytes(), before)
        for suffix in ("-wal", "-shm", "-journal"):
            report = Path(str(stage) + suffix)
            call = subprocess.run([sys.executable, str(script), "stage", "--output", str(stage),
                                   "--plan", str(plan_path), "--report", str(report)], capture_output=True, text=True)
            self.assertEqual(call.returncode, 2)
            self.assertFalse(stage.exists())
            self.assertFalse(report.exists())

    def test_apply_has_no_implicit_schema_migration_and_rolls_back_projection_failure(self):
        with sqlite3.connect(self.target) as conn:
            conn.execute("ALTER TABLE jobs DROP COLUMN started_at")
        plan = self.plan()
        with readonly(self.target) as conn:
            before = fingerprint(conn)
            schema = [tuple(r) for r in conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]
        with patch("memory.history_merge.SourceDependencies.sync", side_effect=RuntimeError("projection failure")):
            with self.assertRaisesRegex(RuntimeError, "projection failure"):
                apply_plan(plan)
        with readonly(self.target) as conn:
            self.assertEqual(fingerprint(conn), before)
            self.assertEqual([tuple(r) for r in conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")], schema)
            self.assertNotIn("started_at", {r[1] for r in conn.execute("PRAGMA table_info(jobs)")})


if __name__ == "__main__":
    unittest.main()
