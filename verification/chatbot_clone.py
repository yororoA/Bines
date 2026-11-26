"""Clone launch and Compose isolation checks; no engine or QQ login required."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import docker_launcher as docker
import start_modules
from common.env import read_env
from napcat import initialize_deploy


class ChatbotClone(unittest.TestCase):
    def test_alias_and_clone_choose_the_expected_launcher(self):
        with patch.dict(os.environ, {"BINES_PROFILE": "full"}), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(sys, "argv", ["bines.py", "--chatbot", "--host"]), \
                    patch.object(start_modules, "_run_host", return_value=0) as host, \
                    patch("website_runtime.WebsiteBridgeSupervisor"):
                self.assertEqual(start_modules.main(), 0)
                self.assertEqual(host.call_args.args[0].profile, "chatbot")
            with patch.object(sys, "argv", ["bines.py", "--profile", "chatbot", "--mode", "clone"]), \
                    patch.object(docker, "available", return_value=True), \
                    patch.object(start_modules, "_run_docker", return_value=0) as launch, \
                    patch("website_runtime.WebsiteBridgeSupervisor") as bridge:
                self.assertEqual(start_modules.main(), 0)
                self.assertEqual(launch.call_args.args[0].mode, "clone")
                bridge.assert_not_called()

    def test_clone_does_not_fall_back_to_host(self):
        args = ["bines.py", "--profile", "chatbot", "--mode", "clone"]
        with patch.object(sys, "argv", args), patch.dict(os.environ), \
                patch.object(docker, "available", return_value=False), \
                patch.object(start_modules, "_run_host") as host, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(start_modules.main(), 1)
            host.assert_not_called()

    @unittest.skipUnless(docker.available(), "需要 Docker Compose CLI 来解析配置")
    def test_compose_does_not_share_state_or_runtime_endpoints(self):
        def render(target):
            result = docker.run(target, "config", "--format", "json", timeout=20)
            self.assertEqual(result.returncode, 0, docker._safe_output(result.stderr))
            return json.loads(result.stdout)

        with tempfile.TemporaryDirectory() as temp, patch.object(docker, "clone_directory", return_value=Path(temp)), \
                patch.dict(os.environ, {"NAPCAT_ACCOUNT": "123456", "NAPCAT_WS_TOKEN": "main-fixture-token",
                                        "BINES_RAG_RPC_PORT": "5560", "COMPOSE_PROJECT_NAME": "bines"}):
            main, clone = render("chatbot"), render("clone")
            self.assertEqual(clone["name"], "bines-clone")
            for section in ("networks", "volumes"):
                main_names = {row["name"] for row in main[section].values()}
                clone_names = {row["name"] for row in clone[section].values()}
                self.assertTrue(main_names.isdisjoint(clone_names))

            def ports(config):
                return {row["published"] for service in config["services"].values() for row in service.get("ports", [])}

            self.assertTrue(ports(main).isdisjoint(ports(clone)))
            for name, service in clone["services"].items():
                with self.subTest(service=name):
                    for mount in service.get("volumes", []):
                        if mount["type"] != "bind":
                            continue
                        source = Path(mount["source"]).resolve()
                        self.assertTrue(source.is_relative_to(Path(temp).resolve()) or source in {
                            docker.ROOT / "thingking" / "models",
                            docker.ROOT / "thingking" / "data",
                            docker.COMPOSE_FILE.parent / "empty-mcp.json",
                        }, source)
                    self.assertEqual(service["image"], main["services"][name]["image"])
            self.assertEqual(clone["services"]["napcat"]["environment"]["ACCOUNT"], "")
            self.assertEqual(clone["services"]["chatbot"]["environment"]["BINES_NAPCAT_ACCESS_TOKEN"], "")
            for name in ("chatbot", "qq-thinking", "memory-server", "memory-worker", "rag-web"):
                self.assertEqual(clone["services"][name]["environment"]["BINES_MODE"], "clone")
                self.assertNotEqual(main["services"][name]["environment"].get("BINES_MODE"), "clone")

            napcat_dir = Path(temp) / "napcat"
            initialize_deploy(napcat_dir, {"NAPCAT_WS_PORT": "16101", "NAPCAT_WEBUI_PORT": "16099"})
            values = read_env(napcat_dir / ".env")
            initialized = render("clone")
            env = initialized["services"]["chatbot"]["environment"]
            self.assertTrue(env["BINES_NAPCAT_ACCESS_TOKEN"] == values["NAPCAT_WS_TOKEN"] != "main-fixture-token")
            self.assertEqual(env["BINES_MEMORY_BACKEND"], "remote")


if __name__ == "__main__":
    unittest.main()
