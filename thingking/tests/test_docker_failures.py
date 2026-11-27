"""Docker failure diagnostics use fake command output and private temporary logs."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import helpers
import docker_launcher as docker


def result(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class DockerFailureTests(unittest.TestCase):
    def test_cause_survives_long_progress_and_both_output_streams(self):
        cause = "dependency failed to start: container bines-chatbot-1 is unhealthy"
        stdout = "PermissionError: gateway database directory is read-only\n" + "Creating service\n" * 40
        stderr = "\x1b[31m" + cause + "\x1b[0m\r" + "Waiting for dependencies\n" * 40
        with self.assertRaises(docker.DockerError) as raised:
            docker._check_result(result(stdout, stderr, 1), "Docker 启动失败")
        message = docker.format_failure(raised.exception)
        self.assertIn(cause, message)
        self.assertIn("database directory is read-only", message)
        self.assertEqual(message.count("Docker 启动失败"), 1)
        self.assertNotIn("\x1b", message)
        self.assertIn("Creating service", raised.exception.details)

    def test_redaction_preserves_errors_without_credentials_or_environment_dump(self):
        text = ("RuntimeError: failed TOKEN=fixture-token PASSWORD='fixture password'\n"
                'RuntimeError: {"api_key":"fixture-json-key"}\n'
                "Authorization: Bearer fixture-bearer\n"
                "ERROR https://user:fixture-pass@registry.invalid/path?access_token=fixture-query&x=1\n"
                "ERROR --webui-token fixture-flag\n"
                "environment={'BINES_NAME':'fixture-env-value'}\n"
                'ERROR {"Env":["FIXTURE=fixture-json-env"]}\n')
        clean = docker._safe_output(text)
        for secret in ("fixture-token", "fixture password", "fixture-json-key", "fixture-bearer",
                       "fixture-pass", "fixture-query", "fixture-flag", "fixture-env-value", "fixture-json-env"):
            self.assertNotIn(secret, clean)
        self.assertIn("RuntimeError", clean)
        self.assertEqual(docker._safe_output(clean), clean)

    def test_failure_snapshot_uses_only_state_and_failed_service_errors(self):
        rows = [{"Service": "chatbot", "Name": "bines-chatbot-1", "State": "running", "Health": "unhealthy"},
                {"Service": "napcat", "Name": "bines-napcat-1", "State": "running", "Health": "healthy"}]
        state = {"ExitCode": 1, "OOMKilled": False, "Error": "", "Health": {"Log": [
            {"ExitCode": 1, "Output": "gateway: unhealthy (ConnectionRefusedError)", "End": "fixture-time"}]}}
        logs = ("chatbot-1 | [QQ消息] fixture-private-chat\n"
                "chatbot-1 | ERROR messages=fixture-private-request\n"
                'chatbot-1 | ERROR {"messages":["fixture-json-chat"]}\n'
                "chatbot-1 | Traceback (most recent call last):\n"
                'chatbot-1 |   File "/app/chatBot/store.py", line 10, in connect\n'
                "chatbot-1 | sqlite3.OperationalError: unable to open database file\n"
                "chatbot-1 | RuntimeError: token=fixture-sensitive-token\n")
        with tempfile.TemporaryDirectory() as folder, patch.object(docker, "ROOT", Path(folder)), \
                patch.object(docker, "run", side_effect=[result(json.dumps(rows)), result(logs)]) as run, \
                patch.object(docker, "_docker", return_value=result(json.dumps(state))) as inspect:
            report = docker.startup_failure_report("chatbot", docker.DockerError("Docker 启动失败：fixture"))
            path, = (Path(folder) / "logs").glob("docker-startup-*.log")
            saved = path.read_text()
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("unable to open database file", report)
        self.assertIn("health_exit=1", saved)
        for hidden in ("fixture-private-chat", "fixture-private-request", "fixture-json-chat", "fixture-sensitive-token"):
            self.assertNotIn(hidden, report + saved)
        self.assertEqual(run.call_args_list[1].args, ("chatbot", "logs", "--tail", "80", "--no-color", "chatbot"))
        inspect.assert_called_once_with("inspect", "--format", "{{json .State}}", "bines-chatbot-1", timeout=10)

    def test_missing_containers_and_collection_failures_do_not_replace_original_cause(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(docker, "ROOT", Path(folder)), \
                patch.object(docker, "run", return_value=result("[]")):
            report = docker.startup_failure_report("chatbot", RuntimeError("original failure"))
            self.assertIn("original failure", report)
            self.assertIn("未找到当前项目容器", report)
        with tempfile.TemporaryDirectory() as folder, patch.object(docker, "ROOT", Path(folder)), \
                patch.object(docker, "run", side_effect=TimeoutError("fixture timeout")):
            report = docker.startup_failure_report("chatbot", RuntimeError("original failure"))
            self.assertIn("original failure", report)
            self.assertIn("诊断不可用（TimeoutError）", report)

    def test_container_removed_during_collection_reports_unavailable_logs(self):
        rows = [{"Service": "chatbot", "Name": "bines-chatbot-1", "State": "running", "Health": "unhealthy"}]
        with patch.object(docker, "run", side_effect=[result(json.dumps(rows)), result(code=1)]), \
                patch.object(docker, "_docker", return_value=result(code=1)):
            details = docker._startup_diagnostics("chatbot")
        self.assertIn("容器状态已不可用", details)
        self.assertIn("服务日志不可用", details)

    def test_startup_report_precedes_cleanup_and_scan_hint_precedes_up(self):
        import start_modules
        events, output = [], StringIO()
        def fail_up(profile):
            self.assertIn("python napcat.py logs", output.getvalue())
            self.assertIn("管理页不显示 QQ 登录二维码", output.getvalue())
            events.append("up")
            raise docker.DockerError("Docker 启动失败：fixture")
        with patch.dict(os.environ), patch.object(docker, "ensure_engine"), \
                patch.object(docker, "running_services", return_value=set()), \
                patch.object(docker, "up", side_effect=fail_up), \
                patch.object(docker, "startup_failure_report", side_effect=lambda *a, **k: events.append("diagnostic") or "fixture cause"), \
                patch.object(docker, "down", side_effect=lambda **k: events.append("down")), \
                redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(start_modules._run_docker(SimpleNamespace(profile="chatbot")), 1)
        self.assertEqual(events, ["up", "diagnostic", "down"])

    def test_ready_message_does_not_claim_qq_login(self):
        import start_modules
        output = StringIO()
        with patch.dict(os.environ), patch.object(docker, "ensure_engine"), \
                patch.object(docker, "running_services", return_value=set()), patch.object(docker, "up"), \
                patch.object(docker, "wait_ready"), patch.object(docker, "follow"), patch.object(docker, "down"), \
                redirect_stdout(output):
            self.assertEqual(start_modules._run_docker(SimpleNamespace(profile="chatbot")), 0)
        self.assertIn("Docker 服务已就绪", output.getvalue())
        self.assertIn("服务就绪不代表 QQ 已登录", output.getvalue())


if __name__ == "__main__":
    unittest.main()
