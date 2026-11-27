"""Isolated settings/document API checks; never read the installed .env/persona.

Run: BINES_ENV_FILE=/dev/null .venv/bin/python verification/admin_settings.py
"""
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask
from common.admin_settings import MASK, SettingsStore, create_settings_blueprint, settings_schema
from common.env import read_env


def persona_fixture():
    return {
        "identity": {"name": "Test", "aliases": ["T"], "model": "Test-1", "full_name": "Test persona", "species": "virtual",
                     "birthday": "2000-01-01", "height": "1", "native_language": "Chinese", "languages": ["Chinese"], "qq_name": "Test", "situation": "Test"},
        "appearance": {"hair": "a", "eyes": "b", "expression": "c", "outfits": {"daily": "a", "home": "b"}},
        "personality": {"core": ["kind"], "likes": ["tests"], "quirks": []},
        "capability_boundaries": {"refuse_topics": [], "refuse_style": "kind", "still_help_with": "tests"},
        "reactions": {"praised": "a", "insulted": "b", "teased": "c", "name_mistaken": "d"},
        "speech_style": ["brief"], "output_format": ["plain"],
        "voice_guard": {"directives": ["brief"], "examples": [{"user": "hi", "assistant": "hello"}]},
        "proactive": {"relationship_moods": {"friend": "happy"}}, "diary": {"voice": "brief"},
    }


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bines-admin-settings-")
        self.root = Path(self.temp.name).resolve()
        environment = patch.dict(os.environ, {"BINES_ENV_FILE": os.devnull, "BINES_MCP_CONFIG": "mcp.servers.json"})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = SettingsStore(self.root)
        self.app = Flask(__name__)
        self.app.register_blueprint(create_settings_blueprint(self.root))
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def settings(self):
        response = self.client.get("/api/control/settings")
        self.assertEqual(response.status_code, 200)
        return response.json["result"]

    def put_settings(self, changes=None, unset=None, revision=None):
        return self.client.put("/api/control/settings", json={"revision": revision or self.settings()["revision"], "changes": changes or {}, "unset": unset or []})

    def document(self, name):
        response = self.client.get("/api/control/documents/" + name)
        self.assertEqual(response.status_code, 200)
        return response.json["result"]

    def put_document(self, name, content, revision=None):
        return self.client.put("/api/control/documents/" + name, json={"revision": revision or self.document(name)["revision"], "content": content})

    def test_schema_covers_all_example_assignments_including_comments(self):
        sample_keys = set(re.findall(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]+)\s*=", (ROOT / ".env.example").read_text(), re.M))
        self.assertTrue(sample_keys <= settings_schema().keys())
        self.assertTrue({"BINES_CONSOLIDATION_WAIT", "BINES_QDRANT_PORT", "NAPCAT_IMAGE"} <= settings_schema().keys())
        self.assertIn("HEARING_LM_MODEL_REVISION", settings_schema())
        self.assertNotIn("PATH", settings_schema())

    def test_environment_preserves_comments_unknown_keys_and_secret(self):
        original = "# header\r\nexport DEEPSEEK_MODEL = old  # useful\r\nUNKNOWN_PRIVATE='retain-me'\r\nDEEPSEEK_API_KEY='fixture-api-secret'\r\n"
        (self.root / ".env").write_bytes(original.encode())
        response = self.put_settings({"DEEPSEEK_MODEL": "new model", "BINES_QQ_WORKERS": 7})
        self.assertEqual(response.status_code, 200)
        saved = (self.root / ".env").read_bytes().decode()
        self.assertIn("# header\r\n", saved)
        self.assertIn("export DEEPSEEK_MODEL = 'new model'  # useful\r\n", saved)
        self.assertIn("UNKNOWN_PRIVATE='retain-me'\r\n", saved)
        self.assertEqual(read_env(self.root / ".env")["DEEPSEEK_API_KEY"], "fixture-api-secret")
        self.assertNotIn("fixture-api-secret", response.get_data(as_text=True))
        self.assertNotIn("retain-me", response.get_data(as_text=True))
        self.assertTrue(response.json["result"]["restart_required"])

    def test_secret_masks_include_authenticated_urls_and_process_snapshot(self):
        (self.root / ".env").write_text("DEEPSEEK_API_KEY=fixture-secret\nBINES_AUTONOMY_REDIS_URL=redis://user:fixture-password@localhost:6379/0\n")
        previous = os.environ.get("MOMENTS_TOKEN")
        os.environ["MOMENTS_TOKEN"] = "fixture-process-token"
        try:
            result = self.settings()
            body = json.dumps(result)
            self.assertNotIn("fixture-secret", body)
            self.assertNotIn("fixture-password", body)
            self.assertNotIn("fixture-process-token", body)
            fields = {f["key"]: f for g in result["groups"] for f in g["fields"]}
            self.assertTrue(fields["BINES_AUTONOMY_REDIS_URL"]["secret"])
            self.assertIsNone(fields["DEEPSEEK_API_KEY"]["value"])
            self.assertFalse(fields["MOMENTS_TOKEN"]["runtime"]["verified"])
        finally:
            if previous is None:
                os.environ.pop("MOMENTS_TOKEN", None)
            else:
                os.environ["MOMENTS_TOKEN"] = previous

    def test_revision_conflict_and_explicit_unset_blank(self):
        initial = self.settings()["revision"]
        self.assertEqual(self.put_settings({"DEEPSEEK_API_KEY": "fixture-secret"}, revision=initial).status_code, 200)
        self.assertEqual(self.put_settings({"DEEPSEEK_MODEL": "lost"}, revision=initial).status_code, 409)
        self.assertEqual(self.put_settings({"DEEPSEEK_API_KEY": ""}).status_code, 200)
        self.assertEqual(read_env(self.root / ".env")["DEEPSEEK_API_KEY"], "")
        self.assertEqual(self.put_settings(unset=["DEEPSEEK_API_KEY"]).status_code, 200)
        self.assertNotIn("DEEPSEEK_API_KEY", read_env(self.root / ".env"))

    def test_validation_readonly_unknown_and_storage_isolation(self):
        for change in ({"PATH": "/evil"}, {"BINES_ENV_FILE": "evil"}, {"BINES_QQ_WORKERS": 0},
                       {"BINES_QQ_TEMPERATURE": 3}, {"BINES_MANAGER_PORT": 65536}, {"DEEPSEEK_THINKING": "bad"},
                       {"BINES_MEMORY_TIMEZONE": "Invalid/Zone"}, {"DEEPSEEK_MODEL": "x\nINJECT=yes"},
                       {"BINES_QQ_WORKERS": "password-is-private"}):
            response = self.put_settings(change)
            self.assertEqual(response.status_code, 400, change)
            self.assertNotIn("password-is-private", response.get_data(as_text=True))
        self.assertFalse((self.root / ".env").exists())
        self.assertEqual(self.settings()["source"]["env_file"], str(self.root / ".env"))

    def test_existing_duplicates_unset_all_and_interpolation(self):
        (self.root / ".env").write_text("DEEPSEEK_MODEL=first\nDEEPSEEK_MODEL=last\n")
        model = "${BINES_TEST_PREFIX:-fallback}-model"
        self.assertEqual(self.put_settings({"DEEPSEEK_MODEL": model}).status_code, 200)
        self.assertEqual(read_env(self.root / ".env")["DEEPSEEK_MODEL"], "fallback-model")
        self.assertEqual(self.put_settings(unset=["DEEPSEEK_MODEL"]).status_code, 200)
        self.assertNotIn("DEEPSEEK_MODEL", (self.root / ".env").read_text())

    def test_mcp_masks_preserves_replaces_removes_and_conflicts(self):
        payload = {"other": {"preserved": True}, "mcpServers": {"test": {"url": "https://example.test/mcp", "headers": {"Authorization": "fixture-bearer"}, "env": {"TOKEN": "fixture-env"}, "custom": 1}}}
        self.assertEqual(self.put_document("mcp", payload).status_code, 200)
        result = self.document("mcp")
        self.assertEqual(result["content"]["mcpServers"]["test"]["headers"]["Authorization"], MASK)
        self.assertEqual(len(result["secret_paths"]), 2)
        self.assertNotIn("fixture-bearer", json.dumps(result))
        content = result["content"]
        content["mcpServers"]["test"]["timeout"] = 12
        self.assertEqual(self.put_document("mcp", content, result["revision"]).status_code, 200)
        saved = json.loads((self.root / "mcp.servers.json").read_text())
        self.assertEqual(saved["mcpServers"]["test"]["headers"]["Authorization"], "fixture-bearer")
        self.assertTrue(saved["other"]["preserved"])
        self.assertEqual(self.put_document("mcp", content, result["revision"]).status_code, 409)
        del content["mcpServers"]["test"]["env"]["TOKEN"]
        content["mcpServers"]["test"]["headers"]["Authorization"] = "fixture-replacement"
        self.assertEqual(self.put_document("mcp", content).status_code, 200)
        self.assertEqual(self.document("mcp")["secret_paths"], ["/mcpServers/test/headers/Authorization"])

    def test_mcp_rejects_placeholder_on_new_path_and_invalid_structure(self):
        self.assertEqual(self.put_document("mcp", {"mcpServers": {"test": {"url": "https://example.test", "env": {"TOKEN": MASK}}}}).status_code, 400)
        self.assertEqual(self.put_document("mcp", {"mcpServers": {"test": {"command": "test", "args": "invalid"}}}).status_code, 400)

    def test_custom_mcp_uses_selected_document_and_masks_arguments(self):
        with patch.dict(os.environ, {"BINES_MCP_CONFIG": "configs/work.json", "BINES_ADMIN_LOCAL_PROXY": "docker"}):
            store = SettingsStore(self.root)
            self.assertEqual(store.settings()["source"]["mode"], "docker-host")
            loaded = store.document("mcp")
            self.assertEqual(loaded["path"], "configs/work.json")
            server = {"command": "demo", "args": ["--token", "fixture-args-secret"]}
            result = store.update_document("mcp", {"revision": loaded["revision"], "content": {"mcpServers": {"test": server}}})
            self.assertTrue(result["restart_required"])
            self.assertNotIn("fixture-args-secret", json.dumps(result))
            self.assertEqual(result["content"]["mcpServers"]["test"]["args"], MASK)
            result["content"]["mcpServers"]["test"]["timeout"] = 15
            store.update_document("mcp", {key: result[key] for key in ("revision", "content")})
            saved = json.loads((self.root / loaded["path"]).read_text())
            self.assertEqual(saved["mcpServers"]["test"]["args"], server["args"])
            self.assertFalse((self.root / "mcp.servers.json").exists())

    def test_screen_normalization_is_strict_and_atomic(self):
        valid = self.document("screen")["content"]
        valid.update(enabled=True, interval_sec=8, min_change_ratio=0.3)
        self.assertEqual(self.put_document("screen", valid).status_code, 200)
        old = (self.root / "server/realtime_screen_config.json").read_bytes()
        for key, value in (("enabled", "yes"), ("interval_sec", 1), ("interval_sec", 3.5), ("quality", []), ("capture_fps", 13), ("unknown", 1)):
            broken = copy.deepcopy(valid)
            broken[key] = value
            self.assertEqual(self.put_document("screen", broken).status_code, 400, key)
            self.assertEqual((self.root / "server/realtime_screen_config.json").read_bytes(), old)

    def test_skills_preserve_extra_fields_and_validate(self):
        content = {"metadata": {"schema": 2}, "actions": [{"name": "example", "enabled": False, "description": "fixture", "extra": [1]}]}
        self.assertEqual(self.put_document("skills", content).status_code, 200)
        self.assertEqual(self.document("skills")["content"], content)
        content["actions"].append(content["actions"][0])
        self.assertEqual(self.put_document("skills", content).status_code, 400)

    def test_persona_required_model_structure_and_yaml(self):
        content = persona_fixture()
        self.assertEqual(self.put_document("persona", content).status_code, 200)
        self.assertEqual(self.document("persona")["content"], content)
        saved = (self.root / "thingking/persona/persona.yaml").read_bytes()
        content["identity"]["model"] = ""
        self.assertEqual(self.put_document("persona", content).status_code, 400)
        self.assertEqual((self.root / "thingking/persona/persona.yaml").read_bytes(), saved)

    def test_corrupt_files_and_path_escape_cannot_be_overwritten(self):
        (self.root / "mcp.servers.json").write_text("{invalid")
        self.assertEqual(self.client.get("/api/control/documents/mcp").status_code, 400)
        self.assertEqual(self.client.get("/api/control/documents/unknown").status_code, 400)
        with tempfile.TemporaryDirectory(prefix="bines-admin-outside-") as outside:
            target = Path(outside) / "env"
            target.write_text("DEEPSEEK_MODEL=outside\n")
            (self.root / ".env").symlink_to(target)
            self.assertEqual(self.client.get("/api/control/settings").status_code, 400)
            self.assertEqual(target.read_text(), "DEEPSEEK_MODEL=outside\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
