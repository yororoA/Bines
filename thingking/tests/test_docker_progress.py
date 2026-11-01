from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import helpers
import docker_launcher
from common.docker_progress import BuildProgress, stream_build


class DockerProgressTests(unittest.TestCase):
    def test_step_is_visible_before_subprocess_finishes_and_full_log_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / "finished"
            seen_before_finish = []
            class Output(StringIO):
                def write(self, text):
                    if "安装 Python 依赖" in text:
                        seen_before_finish.append(not marker.exists())
                    return super().write(text)
            output = Output()
            code = (
                "import time;from pathlib import Path;"
                "print('#1 [memory-server 6/8] RUN pip install requirements',flush=True);"
                "time.sleep(.2);"
                f"Path({str(marker)!r}).write_text('done');"
                "print('#1 DONE 0.2s',flush=True)"
            )
            with redirect_stdout(output):
                result = stream_build([sys.executable, "-u", "-c", code], cwd=root,
                                      env=dict(os.environ), log_path=root / "build.log", timeout=3)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(seen_before_finish, [True])
            self.assertIn("#1 DONE", (root / "build.log").read_text())
            self.assertIn("构建完成", output.getvalue())

    def test_download_noise_is_throttled_and_silent_steps_have_heartbeat(self):
        now = [0]
        lines = []
        progress = BuildProgress(emit=lambda line, **_: lines.append(line), clock=lambda: now[0])
        progress.consume("#2 [memory-server 6/8] RUN pip install requirements")
        for index in range(100):
            progress.consume(f"#2 0.1 Downloading package{index}-1.0.whl (100 kB)")
        progress.consume("#2 0.2 Downloading torch-2.0.whl (98.9 MB)")
        self.assertLess(len(lines), 5)
        self.assertTrue(any("下载 torch" in line for line in lines))
        now[0] = 11
        progress.heartbeat()
        self.assertIn("00:11", lines[-1])
        self.assertIn("已完成", lines[-1])

    def test_failed_build_returns_exit_status_and_error_tail(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(StringIO()):
            result = stream_build(
                [sys.executable, "-c", "import sys;print('dependency unavailable');sys.exit(7)"],
                cwd=folder, env=dict(os.environ), log_path=Path(folder) / "failed.log",
            )
        self.assertEqual(result.returncode, 7)
        self.assertIn("dependency unavailable", result.stdout)

    def test_timeout_reaps_the_started_process(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(StringIO()):
            root = Path(folder)
            marker = root / "pid"
            code = f"import os,time;from pathlib import Path;Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(20)"
            with self.assertRaises(subprocess.TimeoutExpired):
                stream_build([sys.executable, "-u", "-c", code], cwd=root, env=dict(os.environ),
                             log_path=root / "timeout.log", timeout=.2)
            pid = int(marker.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


class DockerRebuildTests(unittest.TestCase):
    def test_shutdown_keeps_cache_and_does_not_boot_builder_for_pruning(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(docker_launcher, "_builder_driver", return_value="docker-container"), patch.object(
            docker_launcher, "_docker", return_value=ok,
        ) as command:
            docker_launcher.cleanup_build_cache(prune=False)
        command.assert_called_once_with("buildx", "stop", "bines", timeout=60)

    def test_older_buildx_keeps_bounded_cache_instead_of_removing_builder(self):
        unsupported = subprocess.CompletedProcess([], 1, "", "unknown flag: --max-used-space")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(docker_launcher, "_builder_driver", return_value="docker-container"), patch.object(
            docker_launcher, "_docker", side_effect=[unsupported, ok, ok],
        ) as command:
            docker_launcher.cleanup_build_cache()
        calls = [call.args for call in command.call_args_list]
        self.assertIn(("buildx", "prune", "--builder", "bines", "--force", "--keep-storage", "4GB"), calls)
        self.assertEqual(calls[-1], ("buildx", "stop", "bines"))
        self.assertFalse(any("rm" in args for args in calls))

    def test_failed_prune_still_stops_builder_without_unbounded_fallback(self):
        failed = subprocess.CompletedProcess([], 1, "", "cache read failed")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(docker_launcher, "_builder_driver", return_value="docker-container"), patch.object(
            docker_launcher, "_docker", side_effect=[failed, ok],
        ) as command:
            with self.assertRaisesRegex(docker_launcher.DockerError, "cache read failed"):
                docker_launcher.cleanup_build_cache()
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args.args, ("buildx", "stop", "bines"))

    def test_prune_failure_survives_a_second_failure_while_stopping(self):
        for failure in (subprocess.CompletedProcess([], 1, "", "cache read failed"),
                        subprocess.TimeoutExpired(["buildx", "prune"], 180)):
            with self.subTest(failure=type(failure).__name__):
                output = StringIO()
                with patch.object(docker_launcher, "_builder_driver", return_value="docker-container"), patch.object(
                    docker_launcher, "_docker", side_effect=[failure, OSError("stop failed token=fixture-private")],
                ) as command, redirect_stderr(output):
                    with self.assertRaises((docker_launcher.DockerError, subprocess.TimeoutExpired)) as raised:
                        docker_launcher.cleanup_build_cache()
                if isinstance(failure, BaseException):
                    self.assertIs(raised.exception, failure)
                else:
                    self.assertIn("cache read failed", str(raised.exception))
                    self.assertIn("stop failed", raised.exception.details)
                    self.assertNotIn("fixture-private", raised.exception.details)
                self.assertEqual(command.call_args.args, ("buildx", "stop", "bines"))
                self.assertEqual(command.call_count, 2)
                self.assertIn("stop failed", output.getvalue())
                self.assertNotIn("fixture-private", output.getvalue())

    def test_failed_build_or_interrupt_is_not_replaced_by_cache_cleanup_error(self):
        cases = (None, subprocess.TimeoutExpired(["build"], 3600), KeyboardInterrupt())
        for failure in cases:
            with self.subTest(failure=type(failure).__name__):
                failed_result = subprocess.CompletedProcess([], 1, "pip download failed", "")
                output = StringIO()
                with patch.object(docker_launcher, "_stale_services", return_value=["chatbot"]), patch.object(
                    docker_launcher, "ensure_builder",
                ), patch.object(docker_launcher, "run_build", return_value=failed_result, side_effect=failure), patch.object(
                    docker_launcher, "cleanup_build_cache", side_effect=docker_launcher.DockerError("cache cleanup failed"),
                ) as cleanup, patch.object(docker_launcher, "run") as start, redirect_stdout(StringIO()), redirect_stderr(output):
                    with self.assertRaises(BaseException) as raised:
                        docker_launcher.up("chatbot")
                if failure is not None:
                    self.assertIs(raised.exception, failure)
                else:
                    self.assertIsInstance(raised.exception, docker_launcher.DockerError)
                    self.assertIn("pip download failed", str(raised.exception))
                    self.assertIn("cache cleanup failed", raised.exception.details)
                cleanup.assert_called_once_with()
                start.assert_not_called()
                self.assertIn("cache cleanup failed", output.getvalue())

    def test_cleanup_failure_without_a_primary_error_remains_visible(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        stopped = subprocess.CompletedProcess([], 1, "", "could not stop builder")
        with patch.object(docker_launcher, "_builder_driver", return_value="docker-container"), patch.object(
            docker_launcher, "_docker", side_effect=[ok, stopped],
        ):
            with self.assertRaisesRegex(docker_launcher.DockerError, "could not stop builder"):
                docker_launcher.cleanup_build_cache()

    def test_shared_builder_is_never_pruned_or_stopped(self):
        with patch.object(docker_launcher, "_builder_driver", return_value="docker"), patch.object(
            docker_launcher, "_docker",
        ) as command:
            with self.assertRaises(docker_launcher.DockerError):
                docker_launcher.cleanup_build_cache()
        command.assert_not_called()

    def test_browser_and_installer_changes_do_not_rebuild_images_but_memory_changes_do(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = ["common/website_cdp.py", "common/website_observer.py", "common/docker_progress.py",
                     "install.py", "skills/website/SKILL.md", "thingking/src/memory/service.py",
                     "requirements/thinking.txt"]
            for name in files:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("before")
            with patch.object(docker_launcher, "ROOT", root):
                baseline = docker_launcher.source_hashes({})
                for name in files[:4]:
                    (root / name).write_text("host-only change")
                self.assertEqual(docker_launcher.source_hashes({}), baseline)
                (root / "skills/website/SKILL.md").write_text("new QQ tool documentation")
                skill_change = docker_launcher.source_hashes({})
                self.assertNotEqual(skill_change["chatbot"], baseline["chatbot"])
                self.assertEqual(skill_change["memory-server"], baseline["memory-server"])
                (root / "thingking/src/memory/service.py").write_text("memory logic change")
                memory_change = docker_launcher.source_hashes({})
                self.assertNotEqual(memory_change["chatbot"], skill_change["chatbot"])
                self.assertNotEqual(memory_change["memory-server"], skill_change["memory-server"])

    def test_apt_script_and_mirror_changes_invalidate_both_image_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / "deploy/docker/install_apt.py"
            script.parent.mkdir(parents=True)
            script.write_text("# initial apt installer\n")
            with patch.object(docker_launcher, "ROOT", root):
                baseline = docker_launcher.source_hashes({})
                self.assertEqual(baseline, docker_launcher.source_hashes({
                    "BINES_APT_MIRROR": "https://mirrors.tuna.tsinghua.edu.cn"}))
                script.write_text("# bounded apt retry update\n")
                script_change = docker_launcher.source_hashes({})
                mirror_change = docker_launcher.source_hashes({"BINES_APT_MIRROR": "https://apt.fixture.invalid"})
                for service in docker_launcher.BUILD_SERVICES:
                    self.assertNotEqual(baseline[service], script_change[service])
                    self.assertNotEqual(script_change[service], mirror_change[service])

    def test_launcher_uses_streaming_build_and_keeps_existing_cleanup(self):
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(docker_launcher, "_stale_services", return_value=["chatbot"]), patch.object(
            docker_launcher, "ensure_builder",
        ), patch.object(docker_launcher, "run_build", return_value=result) as build, patch.object(
            docker_launcher, "run", return_value=result,
        ), patch.object(docker_launcher, "cleanup_build_cache") as cleanup, patch.object(
            docker_launcher, "cleanup_images",
        ), redirect_stdout(StringIO()):
            docker_launcher.up("chatbot")
        build.assert_called_once_with("chatbot", "chatbot")
        cleanup.assert_called_once()


class DockerReadinessTests(unittest.TestCase):
    def test_worker_or_missing_health_must_not_report_startup_complete(self):
        services = docker_launcher.REQUIRED_SERVICES["chatbot"]
        ready = {name: {"State": "running", "Health": "healthy"} for name in services}
        for name in ("qq-thinking", "memory-worker", "memory-server"):
            for health in ("", "starting", "unhealthy"):
                with self.subTest(service=name, health=health):
                    pending = {**ready, name: {"State": "running", "Health": health}}
                    with patch.object(docker_launcher, "_ps", side_effect=[pending, ready]) as statuses, patch(
                        "time.sleep",
                    ), patch("time.monotonic", side_effect=[0, 0, 0]):
                        docker_launcher.wait_ready("chatbot", timeout=1)
                    self.assertEqual(statuses.call_count, 2)


if __name__ == "__main__":
    unittest.main()
