"""用临时目录和 Docker 回执验证重置，不接触实际实例数据。"""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common.memory_reset import PERSONA_ONLY_MARKER, pending_path
import docker_launcher as docker
import start_modules


class MemoryResetTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.original = self.base / "thingking" / "memory_data"
        self.clone = self.base / ".tools" / "chatbot-clone" / "memory_data"
        self.project = docker.CLONE_PROJECT
        self.persona = self.base / "thingking" / "persona" / "persona_clone.yaml"
        self.login = self.clone.parent / "napcat" / "data" / "qq" / "login.json"
        self.files = (self.original / "memory.db", self.persona, self.login)
        for path in (*self.files, self.clone / "memory.db", self.clone / "index" / "vectors",
                     self.clone / "backups" / "old.db", self.clone / "autonomy.db"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        self.volumes = {f"{self.project}-{suffix}" for suffix in ("gateway", "autonomy", "qdrant")}
        self.volumes.add("bines-gateway")
        self.owner = f"label=com.docker.compose.project={self.project}"
        self.running = False
        self.rm_fails = False
        self.rm_calls = []
        for replacement in (patch.object(docker, "ROOT", self.base),
                            patch.object(docker, "_docker", side_effect=self.docker_call),
                            patch.dict(os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""})):
            replacement.start()
            self.addCleanup(replacement.stop)

    def docker_call(self, *args, timeout):
        def ok(output=""):
            return subprocess.CompletedProcess(args, 0, output, "")

        if args[:2] == ("context", "inspect"):
            return ok("unix:///tmp/test.sock\n")
        if args[0] == "ps":
            return ok("running-test-container\n" if args[-1] == self.owner and self.running else "")
        if args[:2] == ("volume", "ls"):
            names = self.volumes
            if "--filter" in args:
                names = names - {"bines-gateway"} if args[-1] == self.owner else names & {"bines-gateway"}
            return ok("\n".join(sorted(names)))
        if args[:2] == ("volume", "rm"):
            self.rm_calls.append(set(args[2:]))
            if self.rm_fails:
                return subprocess.CompletedProcess(args, 1, "", "fixture removal failure")
            self.volumes.difference_update(args[2:])
            return ok()
        raise AssertionError(args)

    def test_cli_confirmation_selects_only_clone_data_and_preserves_persona(self):
        args = ["bines.py", "--profile", "chatbot", "--mode", "clone", "--reset-memory"]
        with patch.object(sys, "argv", args), patch.object(docker, "available", return_value=True), \
                redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=["bines", self.project]):
            self.assertEqual(start_modules.main(), 1)
            self.assertTrue((self.clone / "memory.db").exists())
            self.assertFalse(self.rm_calls)
            self.assertEqual(start_modules.main(), 0)
        self.assertEqual({p.name for p in self.clone.iterdir()}, {PERSONA_ONLY_MARKER})
        self.assertEqual(self.volumes, {"bines-gateway"})
        self.assertFalse(pending_path(self.clone).exists())
        self.assertTrue(all(p.read_text(encoding="utf-8") == "fixture" for p in self.files))

    def test_reset_main_does_not_touch_clone(self):
        with patch("process_runtime.find_port_listeners", return_value={}):
            docker.reset_memory("chatbot")
        self.assertEqual({p.name for p in self.original.iterdir()}, {PERSONA_ONLY_MARKER})
        self.assertEqual(self.rm_calls, [{"bines-gateway"}])
        self.assertTrue((self.clone / "memory.db").exists())
        self.assertTrue(self.persona.exists())
        self.assertTrue(self.login.exists())

    def test_reset_requires_no_target_containers(self):
        self.running = True
        with self.assertRaisesRegex(docker.DockerError, "--docker-down"):
            docker.reset_memory("clone")
        self.assertTrue((self.clone / "memory.db").exists())
        self.assertFalse(self.rm_calls)

    def test_failed_volume_cleanup_blocks_start_until_a_retry_succeeds(self):
        self.rm_fails = True
        with self.assertRaisesRegex(docker.DockerError, "删除实例数据卷失败"):
            docker.reset_memory("clone")
        with self.assertRaisesRegex(docker.DockerError, "上次重置未完成"):
            docker.check_reset_complete("clone")
        self.assertTrue(any(pending_path(self.clone).glob("*/memory.db")))
        self.rm_fails = False
        docker.reset_memory("clone")
        docker.check_reset_complete("clone")
        self.assertEqual(self.volumes, {"bines-gateway"})
        self.assertEqual({p.name for p in self.clone.iterdir()}, {PERSONA_ONLY_MARKER})


class PersonaOnlyBootTest(unittest.TestCase):
    def test_reset_skips_bootstrap_corpus_without_disabling_vector_store(self):
        from memory.server import build_service

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = root / "corpus.jsonl"
            corpus.touch()
            settings = SimpleNamespace(data_dir=root, db_path=root / "memory.db", index_dir=root / "index")
            with patch("memory.settings.load_settings", return_value=settings), \
                    patch("memory.embedding.load_embedder"), patch("memory.embedding.load_expression_embedder"), \
                    patch("memory.repository.MemoryRepository"), patch("memory.vector_index.VectorIndex"), \
                    patch("memory.server.MemoryService") as service, \
                    patch("memory.vector_maintenance.VectorMaintenance"), \
                    patch("memory.expression_vector.ExpressionVectorStore") as store, \
                    patch("memory.expression_vector.load_corpus", return_value=["seed"]), \
                    patch("config.EXPRESSION_CORPUS_PATH", str(corpus)):
                store.return_value.count.return_value = 0
                build_service()
                store.return_value.index.assert_called_once_with(["seed"], "owner")
                store.return_value.index.reset_mock()
                (root / PERSONA_ONLY_MARKER).touch()
                build_service()
                store.return_value.index.assert_not_called()
                self.assertIs(service.call_args.kwargs["expression_vector"], store.return_value)


if __name__ == "__main__":
    unittest.main()
