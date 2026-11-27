"""APT source/failure fixtures only; never contact repositories or install packages."""
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

from deploy.docker import install_apt as apt


SOURCES = """Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs:
 http://security.debian.org/debian-security
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""


class AptInstallerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="bines apt fixture ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sources = self.root / "etc/apt"
        (self.sources / "sources.list.d").mkdir(parents=True)
        self.source = self.sources / "sources.list.d/debian.sources"
        self.source.write_text(SOURCES)
        self.lists = self.root / "lists"
        self.lists.mkdir()
        self.calls = []

    def install(self, failures=(), mirror="mirror.invalid"):
        def runner(arguments, *, timeout, env):
            stage = "update" if arguments[-1] == "update" else "install"
            source = self.source.read_text()
            endpoint = "official" if "https://deb.debian.org/debian" in source else "mirror"
            self.calls.append((endpoint, stage, list(arguments), timeout, env, source))
            if stage == "update":
                self.assertEqual(list(self.lists.iterdir()), [])
                (self.lists / "partial-index").write_text("simulated partial metadata")
            if (endpoint, stage) in failures:
                raise apt.AptInstallError("controlled connection failure; status 100")
        output = StringIO()
        with redirect_stdout(output):
            apt.install_packages(["ca-certificates", "ffmpeg"], mirror=mirror, source_root=self.sources,
                                 lists_root=self.lists, runner=runner)
        return output.getvalue()

    def test_deb822_preserves_security_suites_keys_and_other_repositories(self):
        other = "\nTypes: deb\nURIs: https://vendor.invalid/repo\nSuites: stable\n"
        text = apt.rewrite_source(SOURCES + other, deb822=True, mirror="https://mirror.invalid/prefix")
        self.assertIn("https://mirror.invalid/prefix/debian\n", text)
        self.assertIn(" https://mirror.invalid/prefix/debian-security\n", text)
        self.assertIn("Suites: trixie-security", text)
        self.assertIn("Suites: trixie trixie-updates", text)
        self.assertEqual(text.count("Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg"), 2)
        self.assertTrue(text.endswith(other))

    def test_legacy_lists_keep_options_and_security_path(self):
        text = ("# deb http://deb.debian.org/debian ignored main\n"
                "deb [arch=arm64 signed-by=/keyring.gpg] http://deb.debian.org/debian stable main\n"
                "deb-src http://security.debian.org/debian-security stable-security main\n")
        rewritten = apt.rewrite_source(text, deb822=False, mirror="https://mirror.invalid")
        self.assertTrue(rewritten.startswith(text.splitlines(keepends=True)[0]))
        self.assertIn("[arch=arm64 signed-by=/keyring.gpg] https://mirror.invalid/debian", rewritten)
        self.assertIn("deb-src https://mirror.invalid/debian-security stable-security main", rewritten)

    def test_mirror_values_upgrade_http_and_reject_credentials_or_queries(self):
        for value in ("mirror.invalid", "http://mirror.invalid", "https://mirror.invalid/"):
            self.assertEqual(apt.mirror_url(value), "https://mirror.invalid")
        self.assertEqual(apt.mirror_url("https://mirror.invalid/prefix/"), "https://mirror.invalid/prefix")
        for value in ("", "ftp://mirror.invalid", "https://u:p@mirror.invalid", "https://mirror.invalid?",
                      "https://mirror.invalid?q=x", "https://mirror.invalid#x", "https://[invalid", "bad host"):
            with self.subTest(value=value), self.assertRaises(apt.AptInstallError):
                apt.mirror_url(value)

    def test_success_uses_https_bounded_settings_and_signature_verification(self):
        self.assertIn("succeeded", self.install())
        self.assertEqual([(x[0], x[1]) for x in self.calls], [("mirror", "update"), ("mirror", "install")])
        self.assertEqual([x[3] for x in self.calls], [120, 600])
        for _, _, command, _, env, source in self.calls:
            self.assertIn("Acquire::Retries=2", command)
            self.assertIn("Acquire::https::Timeout=20", command)
            self.assertIn("APT::Update::Error-Mode=any", command)
            self.assertIn("Acquire::AllowInsecureRepositories=false", command)
            self.assertIn("APT::Get::AllowUnauthenticated=false", command)
            self.assertIn("Acquire::https::Verify-Peer=true", command)
            self.assertNotIn("--allow-unauthenticated", command)
            self.assertEqual(env["DEBIAN_FRONTEND"], "noninteractive")
            self.assertNotIn("http://", source)
        self.assertEqual(list(self.lists.iterdir()), [])

    def test_failed_update_uses_official_sources_before_installing(self):
        log = self.install({("mirror", "update")})
        self.assertEqual([(x[0], x[1]) for x in self.calls],
                         [("mirror", "update"), ("official", "update"), ("official", "install")])
        self.assertIn("update failed", log)
        self.assertIn("official Debian HTTPS sources", log)
        self.assertIn("https://security.debian.org/debian-security", self.calls[-1][-1])
        self.assertIn("Signed-By:", self.calls[-1][-1])

    def test_partial_package_download_failure_retries_from_clean_official_metadata(self):
        log = self.install({("mirror", "install")})
        self.assertEqual([(x[0], x[1]) for x in self.calls],
                         [("mirror", "update"), ("mirror", "install"), ("official", "update"), ("official", "install")])
        self.assertIn("install failed", log)
        self.assertIn("succeeded", log)

    def test_all_sources_failing_is_not_reported_as_success(self):
        with self.assertRaisesRegex(apt.AptInstallError, "All APT source attempts failed"):
            self.install({("mirror", "install"), ("official", "install")})
        self.assertEqual(len(self.calls), 4)

    def test_existing_https_official_source_is_attempted_once(self):
        self.source.write_text("Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\nComponents: main\n")
        self.install(mirror="https://deb.debian.org")
        self.assertEqual(len(self.calls), 2)

    def test_unsupported_sources_and_package_options_fail_before_apt(self):
        runner = Mock()
        self.source.write_text("Types: deb\nURIs: https://vendor.invalid/repo\nSuites: stable\n")
        for packages in (["ffmpeg"], ["--allow-unauthenticated"], []):
            with self.assertRaises(apt.AptInstallError):
                apt.install_packages(packages, source_root=self.sources, lists_root=self.lists, runner=runner)
        runner.assert_not_called()

    @unittest.skipUnless(hasattr(os, "killpg"), "APT process groups require a POSIX runtime")
    def test_timeout_terminates_the_whole_process_group_before_returning(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("fake-apt", .1), 0, 0]
        popen = Mock()
        popen.return_value.__enter__ = Mock(return_value=process)
        popen.return_value.__exit__ = Mock(return_value=False)
        with patch.object(apt.subprocess, "Popen", popen), patch.object(apt.os, "killpg") as kill:
            with self.assertRaisesRegex(apt.AptInstallError, "process group was terminated"):
                apt.run_apt(["fake-apt"], timeout=.1, env={})
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(kill.call_args_list, [call(12345, signal.SIGTERM), call(12345, signal.SIGKILL)])

    def test_real_subprocess_nonzero_status_is_propagated(self):
        with self.assertRaisesRegex(apt.AptInstallError, "status 100"):
            apt.run_apt([sys.executable, "-c", "raise SystemExit(100)"], timeout=5, env=dict(os.environ))

    def test_both_images_use_shared_installer_and_compose_passes_mirror(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        compose = yaml.safe_load((root / "deploy/docker/compose.yaml").read_text())
        for image, service in (("launcher", "chatbot"), ("thinking", "memory-server")):
            dockerfile = (root / f"deploy/docker/Dockerfile.{image}").read_text()
            self.assertIn("COPY deploy/docker/install_apt.py /usr/local/bin/bines-install-apt.py", dockerfile)
            self.assertIn('--mirror "${APT_MIRROR}"', dockerfile)
            self.assertNotIn("sed -i", dockerfile)
            self.assertEqual(compose["services"][service]["build"]["args"]["APT_MIRROR"],
                             "${BINES_APT_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn}")


if __name__ == "__main__":
    unittest.main()
