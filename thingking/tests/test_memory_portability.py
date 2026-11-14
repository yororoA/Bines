"""Real SQLite/WAL and ZIP round trips, using synthetic data only."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "thingking" / "src"))
sys.path.insert(0, str(ROOT))

from memory import portability
from memory.repository import MemoryRepository


class MemoryPortabilityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-memory-portability-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "source" / "memory.db"
        self.repo = MemoryRepository(self.database)
        self.addCleanup(self.repo.close)
        self.repo.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.repo.ensure_conversation("local", "local", "owner")
        self.repo.add_message("local", "owner", "user", "仅用于迁移测试的原话", 100, "2026-11-14")
        self.memory_id = self.repo.add_memory("semantic", "owner", "测试记忆")
        self.repo.put_state("owner", {"mood": "test"})
        # Future modules may add tables without changing the transfer tool.
        self.repo.conn.execute("CREATE TABLE extension_data (id INTEGER PRIMARY KEY, value BLOB)")
        self.repo.conn.execute("INSERT INTO extension_data VALUES(1,?)", (b"\x00\xffbinary",))
        self.repo.conn.execute("INSERT INTO jobs(kind,scope,status,payload,available_at,lease_until,created_at,updated_at) "
                               "VALUES('summarize','owner','running','{\"sample\":true}',0,9999999999,0,0)")
        (self.database.parent / "memory-rpc.token").write_text("synthetic-secret")
        (self.database.parent / "index").mkdir()
        self.archive = self.root / "memory.zip"

    def rewrite(self, name, transform):
        target = self.root / name
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(target, "w") as destination:
            for item in source.infolist():
                data = source.read(item.filename)
                destination.writestr(item, transform(item.filename, data))
        return target

    def test_online_wal_roundtrip_preserves_every_table_and_jobs(self):
        self.assertGreater(Path(str(self.database) + "-wal").stat().st_size, 0)
        exported = portability.export_bundle(self.database, self.archive)
        self.assertEqual(exported["database"]["tables"]["messages"], 1)
        with zipfile.ZipFile(self.archive) as bundle:
            self.assertEqual(set(bundle.namelist()), {"manifest.json", "memory.db"})
        inspected = portability.inspect_bundle(self.archive)
        self.assertEqual(inspected["database"], exported["database"])
        target = self.root / "new-device"
        imported = portability.import_bundle(self.archive, target)
        self.assertEqual(set(p.name for p in target.iterdir()), {"memory.db"})
        self.assertEqual(hashlib.sha256((target / "memory.db").read_bytes()).hexdigest(),
                         exported["database"]["sha256"])
        with sqlite3.connect(imported["database_path"]) as connection:
            self.assertEqual(connection.execute("SELECT content FROM messages").fetchone()[0], "仅用于迁移测试的原话")
            self.assertEqual(connection.execute("SELECT value FROM extension_data").fetchone()[0], b"\x00\xffbinary")
            self.assertEqual(connection.execute("SELECT status,lease_until FROM jobs").fetchone(), ("running", 9999999999))
        self.assertIn("credentials", imported["excluded"])
        self.assertIn("autonomy_redis", imported["excluded"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((target / "memory.db").stat().st_mode), 0o600)

    def test_full_service_schema_and_fts_remain_usable_after_import(self):
        from memory.lexical_index import LexicalIndex
        from memory.service import MemoryService
        from memory.settings import MemorySettings
        settings = MemorySettings(data_dir=self.database.parent, associations_enabled=False)
        service = MemoryService(self.repo, LexicalIndex(self.repo), settings)
        service.sync_identities()
        service.reconcile_index()
        exported = portability.export_bundle(self.database, self.archive)
        self.assertIn("retrieval_keys_fts", exported["database"]["tables"])
        target = self.root / "complete-schema"
        portability.import_bundle(self.archive, target)
        restored = MemoryRepository(target / "memory.db")
        try:
            restored_service = MemoryService(restored, LexicalIndex(restored),
                                             MemorySettings(data_dir=target, associations_enabled=False))
            results = restored_service.search("测试记忆", ["owner"])
            self.assertTrue(any(row.get("id") == self.memory_id for row in results), results)
            self.assertEqual(restored.get_state("owner")["data"]["mood"], "test")
        finally:
            restored.close()

    def test_empty_memory_and_empty_target_are_supported(self):
        self.repo.conn.execute("DELETE FROM memories")
        portability.export_bundle(self.database, self.archive)
        target = self.root / "empty"
        target.mkdir()
        self.assertEqual(portability.import_bundle(self.archive, target)["database"]["tables"]["memories"], 0)

    def test_existing_empty_target_stages_on_its_own_filesystem(self):
        portability.export_bundle(self.database, self.archive)
        target = self.root / "mounted-target"
        target.mkdir()
        temporary_directory = portability.tempfile.TemporaryDirectory
        publish_file = portability._publish_file

        def stage_here(*args, **kwargs):
            self.assertEqual(Path(kwargs["dir"]), target)
            return temporary_directory(*args, **kwargs)

        def same_filesystem(staged, destination):
            # Parent-based staging would cross a filesystem boundary when this
            # directory is a mount point, and would fail this assertion.
            self.assertEqual(staged.parent.parent, target)
            self.assertEqual(destination, target / "memory.db")
            return publish_file(staged, destination)

        with patch.object(portability.tempfile, "TemporaryDirectory", side_effect=stage_here) as stage, \
                patch.object(portability, "_publish_file", side_effect=same_filesystem):
            portability.import_bundle(self.archive, target)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual([entry.name for entry in target.iterdir()], ["memory.db"])

    def test_bad_bundle_leaves_existing_target_empty(self):
        target = self.root / "empty-target"
        target.mkdir()
        self.archive.write_bytes(b"not a zip")
        with self.assertRaises(portability.BundleError):
            portability.import_bundle(self.archive, target)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_concurrent_non_database_file_is_preserved_and_import_refused(self):
        portability.export_bundle(self.database, self.archive)
        extract_verified = portability._extract_verified
        for existing in (False, True):
            target = self.root / f"concurrent-target-{existing}"
            if existing:
                target.mkdir()

            def add_user_data(archive, directory):
                manifest = extract_verified(archive, directory)
                target.mkdir(exist_ok=True)
                (target / "user-notes.txt").write_text("keep user data")
                return manifest

            with self.subTest(existing=existing), \
                    patch.object(portability, "_extract_verified", side_effect=add_user_data), \
                    self.assertRaises(portability.BundleError):
                portability.import_bundle(self.archive, target)
            self.assertEqual([entry.name for entry in target.iterdir()], ["user-notes.txt"])
            self.assertEqual((target / "user-notes.txt").read_text(), "keep user data")

    def test_corruption_is_rejected_before_destination_is_created(self):
        portability.export_bundle(self.database, self.archive)
        def corrupt(name, data):
            if name == "memory.db":
                return data[:-1] + bytes([data[-1] ^ 0xff])
            return data
        broken = self.rewrite("corrupt.zip", corrupt)
        with self.assertRaisesRegex(portability.BundleError, "SHA256"):
            portability.inspect_bundle(broken)
        target = self.root / "must-not-exist"
        with self.assertRaises(portability.BundleError):
            portability.import_bundle(broken, target)
        self.assertFalse(target.exists())

    def test_table_count_and_future_schema_are_verified(self):
        portability.export_bundle(self.database, self.archive)
        for field, value in (("schema_version", portability.SCHEMA_VERSION + 1),
                             ("tables", {"messages": 999})):
            def change(name, data):
                if name == "manifest.json":
                    manifest = json.loads(data)
                    manifest["database"][field] = value
                    return json.dumps(manifest).encode()
                return data
            broken = self.rewrite(field + ".zip", change)
            with self.subTest(field=field), self.assertRaises(portability.BundleError):
                portability.inspect_bundle(broken)
        self.repo.conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        with self.assertRaisesRegex(portability.BundleError, "版本"):
            portability.export_bundle(self.database, self.root / "future.zip")

    def test_existing_outputs_and_target_data_are_never_overwritten(self):
        self.archive.write_bytes(b"existing-output")
        with self.assertRaises(portability.BundleError):
            portability.export_bundle(self.database, self.archive)
        self.assertEqual(self.archive.read_bytes(), b"existing-output")
        for name in ("memory.db", "memory-rpc.token"):
            target = self.root / ("target-" + name)
            target.mkdir()
            (target / name).write_bytes(b"keep")
            with self.subTest(name=name), self.assertRaises(portability.BundleError):
                portability.import_bundle(self.root / "not-even-read.zip", target)
            self.assertEqual((target / name).read_bytes(), b"keep")

    def test_unsafe_members_duplicates_and_symlinks_are_rejected(self):
        portability.export_bundle(self.database, self.archive)
        for name in ("../escape", "/escape", "sub/memory.db", "memory.db"):
            target = self.root / ("unsafe-" + str(len(name)) + "-" + str(abs(hash(name))) + ".zip")
            with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(target, "w") as out:
                for item in source.infolist():
                    out.writestr(item, source.read(item.filename))
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    out.writestr(name, b"bad")
            with self.subTest(name=name), self.assertRaises(portability.BundleError):
                portability.inspect_bundle(target)
        linked = self.root / "symlink.zip"
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(linked, "w") as out:
            for item in source.infolist():
                if item.filename == "memory.db":
                    item.external_attr = (stat.S_IFLNK | 0o777) << 16
                out.writestr(item, source.read(item.filename))
        with self.assertRaisesRegex(portability.BundleError, "文件类型"):
            portability.inspect_bundle(linked)

    def test_import_rejects_symlink_target_and_export_sidecars(self):
        target = self.root / "target-link"
        target.symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaises(portability.BundleError):
            portability.import_bundle(self.root / "unused.zip", target)
        with self.assertRaises(portability.BundleError):
            portability.export_bundle(self.database, Path(str(self.database) + "-journal"))

    def test_size_limit_and_snapshot_timeout_leave_no_output(self):
        with patch.object(portability, "MAX_DATABASE_BYTES", 1024):
            with self.assertRaisesRegex(portability.BundleError, "GiB"):
                portability.export_bundle(self.database, self.archive)
        self.assertFalse(self.archive.exists())
        with patch.object(portability.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(portability.BundleError, "超时"):
                portability.export_bundle(self.database, self.archive)
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.repo.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_validation_timeout_is_bounded(self):
        portability.export_bundle(self.database, self.archive)
        with patch.object(portability, "VALIDATION_TIMEOUT_SECONDS", -1):
            with self.assertRaisesRegex(portability.BundleError, "校验超时"):
                portability.inspect_bundle(self.archive)

    def test_publication_race_does_not_replace_destination(self):
        portability.export_bundle(self.database, self.archive)
        original = portability._publish_file
        def race(staged, target):
            target.write_bytes(b"concurrent-live-database")
            return original(staged, target)
        target = self.root / "raced"
        with patch.object(portability, "_publish_file", side_effect=race):
            with self.assertRaises(portability.BundleError):
                portability.import_bundle(self.archive, target)
        self.assertEqual((target / "memory.db").read_bytes(), b"concurrent-live-database")

    def test_cli_roundtrip_uses_no_model_or_running_service(self):
        environment = dict(os.environ, BINES_ENV_FILE=str(self.root / "no-env"))
        cli = [sys.executable, str(ROOT / "thingking" / "memory_admin.py")]
        for args in (("export-bundle", str(self.archive), "--db", str(self.database)),
                     ("inspect-bundle", str(self.archive)),
                     ("import-bundle", str(self.archive), "--target-dir", str(self.root / "cli-target"))):
            process = subprocess.run(cli + list(args), env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(json.loads(process.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
