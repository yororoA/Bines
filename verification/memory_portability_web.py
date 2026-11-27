"""Transfer HTTP routes exercise real archives with isolated SQLite data."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from common.admin_auth import install_admin_guard
from common.memory_portability_web import create_portability_blueprint


class PortabilityWebTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        # Use the real schema, without opening production databases or models.
        from thingking.src.memory.repository import MemoryRepository
        self.repo = MemoryRepository(self.root / "memory.db")
        self.repo.add_memory("semantic", "owner", "迁移接口测试记忆")
        self.settings = patch("common.memory_portability_web.load_settings",
                              return_value=SimpleNamespace(db_path=self.root / "memory.db"))
        self.settings.start()
        self.app = Flask(__name__)
        install_admin_guard(self.app, "transfer-test", entrypoints=("/status",),
                            token_provider=lambda: "isolated-test-credential")
        self.app.add_url_rule("/status", view_func=lambda: "test entry")
        self.app.register_blueprint(create_portability_blueprint())
        self.client = self.app.test_client()

    def tearDown(self):
        self.settings.stop()
        self.repo.close()
        self.directory.cleanup()

    def test_authentication_and_cross_origin_guards(self):
        self.assertEqual(self.client.get("/api/memory/bundle").status_code, 401)
        self.assertEqual(self.client.post("/api/memory/bundle/inspect", data=b"bad").status_code, 401)
        self.client.get("/status")
        self.assertEqual(self.client.get("/api/memory/bundle", headers={"Origin": "https://other.invalid"}).status_code, 403)

    def test_download_then_inspect_does_not_modify_database(self):
        self.client.get("/status")
        response = self.client.get("/api/memory/bundle")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        archive = response.data
        response.close()
        inspected = self.client.post("/api/memory/bundle/inspect", data=archive,
                                     content_type="application/zip")
        self.assertEqual(inspected.status_code, 200, inspected.json)
        self.assertTrue(inspected.json["success"])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)
        self.assertFalse((self.root / "upload.zip").exists())

    def test_invalid_or_oversized_upload(self):
        self.client.get("/status")
        self.assertEqual(self.client.post("/api/memory/bundle/inspect", data=b"not a zip").status_code, 400)
        with patch("common.memory_portability_web.MAX_UPLOAD_BYTES", 3):
            response = self.client.post("/api/memory/bundle/inspect", data=b"1234")
        self.assertEqual(response.status_code, 413)

    def test_missing_database_does_not_create_one(self):
        self.client.get("/status")
        missing = self.root / "missing.db"
        with patch("common.memory_portability_web.load_settings", return_value=SimpleNamespace(db_path=missing)):
            self.assertEqual(self.client.get("/api/memory/bundle").status_code, 404)
        self.assertFalse(missing.exists())

    def test_temporary_storage_failure_releases_operation_lock(self):
        self.client.get("/status")
        with patch("common.memory_portability_web.tempfile.TemporaryDirectory", side_effect=OSError("storage unavailable")):
            self.assertEqual(self.client.get("/api/memory/bundle").status_code, 503)
        response = self.client.get("/api/memory/bundle")
        self.assertEqual(response.status_code, 200)
        response.close()


if __name__ == "__main__":
    unittest.main()
