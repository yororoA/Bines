"""Installer native-dependency tests: temporary files, no network or package installs."""
import hashlib
import io
from pathlib import Path
import shlex
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import install


class NativeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bines installer ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.options = SimpleNamespace(profile="full", only=["hearing"], dry_run=False,
                                       cache_dir=None, device="cpu", skip_docker=True)
        self.installer = install.Installer(self.options, root=self.root)
        self.installer.env = {"PATH": "/usr/bin:/bin"}
        self.installer.run = Mock()

    def archive(self, name="portaudio/include/pa_mac_core.h", *, symlink=False):
        destination = self.root / "source.tgz"
        with tarfile.open(destination, "w:gz") as handle:
            entry = tarfile.TarInfo(name)
            if symlink:
                entry.type, entry.linkname = tarfile.SYMTYPE, "../../outside"
                handle.addfile(entry)
            else:
                data = b"/* CoreAudio header */\n"
                entry.size = len(data)
                handle.addfile(entry, io.BytesIO(data))
        return destination

    def build_fixture(self):
        python = self.root / "hearing" / "bin" / "python"
        python.parent.mkdir(parents=True)
        (python.parent / "cmake").touch()
        (python.parent / "ninja").touch()
        self.installer.portaudio_archive = Mock(return_value=self.archive())
        self.installer.portaudio_probe = Mock(return_value=True)

        def run(command, label, **kwargs):
            if "--install" in command:
                stage = Path(kwargs["env"]["DESTDIR"])
                prefix = stage / str(self.installer.managed_portaudio_prefix()).lstrip("/")
                (prefix / "include").mkdir(parents=True)
                (prefix / "include" / "portaudio.h").write_text("new header")
                (prefix / "lib").mkdir()
                (prefix / "lib" / "libportaudio.dylib").write_bytes(b"new library")
        self.installer.run.side_effect = run
        return python

    def test_cached_archive_requires_correct_checksum_and_avoids_download(self):
        directory = self.installer.cache_root / "downloads"
        directory.mkdir()
        archive = directory / Path(install.PORTAUDIO_URL).name
        archive.write_bytes(b"verified cached source")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with patch.object(install, "PORTAUDIO_SHA256", digest):
            self.assertEqual(self.installer.portaudio_archive(), archive)
        self.installer.run.assert_not_called()

    def test_checksum_failure_never_promotes_download(self):
        def download(command, *args, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"corrupt source")
        self.installer.run.side_effect = download
        with patch.object(install.shutil, "which", return_value="/usr/bin/curl"):
            with self.assertRaisesRegex(install.InstallError, "SHA256"):
                self.installer.portaudio_archive()
        self.assertEqual(list((self.installer.cache_root / "downloads").iterdir()), [])

    def test_valid_download_replaces_corrupt_cached_archive(self):
        directory = self.installer.cache_root / "downloads"
        directory.mkdir()
        archive = directory / Path(install.PORTAUDIO_URL).name
        archive.write_bytes(b"old corrupt data")
        data = b"verified source"
        def download(command, *args, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(data)
        self.installer.run.side_effect = download
        with patch.object(install.shutil, "which", return_value="/usr/bin/curl"), \
                patch.object(install, "PORTAUDIO_SHA256", hashlib.sha256(data).hexdigest()):
            self.assertEqual(self.installer.portaudio_archive(), archive)
        self.assertEqual(archive.read_bytes(), data)

    def test_archive_rejects_path_escape_and_links(self):
        for name, symlink in (("../outside", False), ("/tmp/outside", False), ("portaudio/link", True)):
            with self.subTest(name=name):
                archive = self.archive(name, symlink=symlink)
                with self.assertRaisesRegex(install.InstallError, "不安全"):
                    install.Installer.extract_portaudio(archive, self.root / "unpack")
        self.assertFalse((self.root / "outside").exists())

    def test_build_stages_on_same_filesystem_then_adds_header_and_rpath(self):
        python = self.build_fixture()
        self.installer.build_managed_portaudio(python)
        prefix = self.installer.managed_portaudio_prefix()
        self.assertEqual(self.installer.portaudio_prefix, prefix)
        self.assertTrue((prefix / "include" / "portaudio.h").is_file())
        self.assertTrue((prefix / "include" / "pa_mac_core.h").is_file())
        self.assertTrue((prefix / "lib" / "libportaudio.dylib").is_file())
        env = self.installer.native_build_env()
        self.assertIn(f"-I{prefix}/include", shlex.split(env["CFLAGS"]))
        self.assertIn(f"-Wl,-rpath,{prefix}/lib", shlex.split(env["LDFLAGS"]))
        configure = self.installer.run.call_args_list[0].args[0]
        self.assertIn("-DPA_BUILD_STATIC=OFF", configure)
        self.assertIn("-DPA_USE_COREAUDIO=ON", configure)
        self.assertEqual(configure[configure.index("-G") + 1], "Ninja")
        self.assertIn(f"-DCMAKE_MAKE_PROGRAM={python.parent / 'ninja'}", configure)
        self.assertFalse(list(prefix.parent.glob(".portaudio-build-*")))

    def test_failed_post_build_probe_restores_previous_installation(self):
        python = self.build_fixture()
        prefix = self.installer.managed_portaudio_prefix()
        prefix.mkdir(parents=True)
        (prefix / "previous-library").write_bytes(b"preserve")
        self.installer.portaudio_probe.return_value = False
        with self.assertRaisesRegex(install.InstallError, "仍不可链接"):
            self.installer.build_managed_portaudio(python)
        self.assertEqual((prefix / "previous-library").read_bytes(), b"preserve")
        self.assertFalse((prefix / "include").exists())
        self.assertIsNone(self.installer.portaudio_prefix)

    def test_compile_failure_preserves_existing_installation(self):
        python = self.build_fixture()
        prefix = self.installer.managed_portaudio_prefix()
        prefix.mkdir(parents=True)
        (prefix / "previous-library").write_bytes(b"preserve")
        self.installer.run.side_effect = install.InstallError("compiler failure; log")
        with self.assertRaisesRegex(install.InstallError, "compiler failure"):
            self.installer.build_managed_portaudio(python)
        self.assertEqual((prefix / "previous-library").read_bytes(), b"preserve")

    def test_missing_managed_ninja_stops_before_source_download(self):
        python = self.build_fixture()
        (python.parent / "ninja").unlink()
        with self.assertRaisesRegex(install.InstallError, "cmake/ninja"):
            self.installer.build_managed_portaudio(python)
        self.installer.portaudio_archive.assert_not_called()

    def test_working_project_library_reused_without_download(self):
        prefix = self.installer.managed_portaudio_prefix()
        (prefix / "include").mkdir(parents=True)
        (prefix / "include" / "portaudio.h").touch()
        self.installer.portaudio_probe = Mock(return_value=True)
        self.installer.build_managed_portaudio = Mock()
        with patch.object(install.os, "name", "posix"):
            self.installer.prepare_portaudio("python")
        self.installer.build_managed_portaudio.assert_not_called()
        self.assertEqual(self.installer.portaudio_prefix, prefix)

    def test_missing_mac_library_uses_managed_build(self):
        self.installer.portaudio_prefixes = Mock(return_value=())
        self.installer.portaudio_probe = Mock(return_value=False)
        self.installer.build_managed_portaudio = Mock()
        with patch.object(install.sys, "platform", "darwin"), patch.object(install.os, "name", "posix"):
            self.installer.prepare_portaudio("hearing-python")
        self.installer.build_managed_portaudio.assert_called_once_with("hearing-python")

    def test_linux_missing_headers_report_development_package_early(self):
        self.installer.portaudio_prefixes = Mock(return_value=())
        self.installer.portaudio_probe = Mock(return_value=False)
        self.installer.build_managed_portaudio = Mock()
        with patch.object(install.sys, "platform", "linux"), patch.object(install.os, "name", "posix"):
            with self.assertRaisesRegex(install.InstallError, "portaudio19-dev"):
                self.installer.prepare_portaudio("hearing-python")
        self.installer.build_managed_portaudio.assert_not_called()

    def test_windows_wheel_needs_no_system_preparation(self):
        self.installer.portaudio_probe = Mock()
        with patch.object(install.os, "name", "nt"):
            self.installer.prepare_portaudio("hearing-python")
        self.installer.portaudio_probe.assert_not_called()

    def test_missing_compiler_reports_action_without_download(self):
        with patch.object(install.sys, "platform", "darwin"), patch.object(install.shutil, "which", return_value=None):
            with self.assertRaisesRegex(install.InstallError, "xcode-select --install"):
                self.installer.portaudio_probe(self.installer.env)
        self.installer.run.assert_not_called()

    def test_probe_compiles_only_and_preserves_quoted_custom_paths(self):
        prefix = self.root / "custom audio"
        self.installer.env["CFLAGS"] = "-O2"
        env = self.installer.native_build_env(prefix)
        with patch.object(install.sys, "platform", "darwin"), patch.object(install.shutil, "which", return_value="/usr/bin/cc"):
            self.assertTrue(self.installer.portaudio_probe(env))
        command = self.installer.run.call_args.args[0]
        self.assertIn(f"-I{prefix}/include", command)
        self.assertIn("-O2", command)
        self.assertIn("-lportaudio", command)
        self.assertEqual(self.installer.run.call_count, 1)

    def test_portaudio_failure_stops_before_torch_and_module_pip(self):
        self.installer.prepare_venv = Mock(return_value=Path("hearing-python"))
        self.installer.pip = Mock()
        self.installer.prepare_portaudio = Mock(side_effect=install.InstallError("native dependency failed"))
        with patch.object(install.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(install.InstallError, "native dependency failed"):
                self.installer.install_environment("hearing")
        labels = [call.args[2] for call in self.installer.pip.call_args_list]
        self.assertEqual(labels, ["准备安装工具", "准备扩展编译依赖"])

    def test_mac_hearing_bootstraps_ninja_before_managed_build(self):
        self.installer.prepare_venv = Mock(return_value=Path("hearing-python"))
        calls = []
        self.installer.pip = Mock(side_effect=lambda p, packages, label, **kwargs: calls.append(tuple(packages)))
        def prepare(python):
            self.assertIn("ninja>=1.11,<2", calls[-1])
            raise install.InstallError("stop before real package installation")
        self.installer.prepare_portaudio = Mock(side_effect=prepare)
        with patch.object(install.sys, "platform", "darwin"), patch.object(install.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(install.InstallError, "stop before"):
                self.installer.install_environment("hearing")

    def test_retry_passes_project_native_flags_to_pyaudio_install(self):
        python = self.root / "hearing-python"
        prefix = self.installer.managed_portaudio_prefix()
        self.installer.prepare_venv = Mock(return_value=python)
        self.installer.pip = Mock()
        self.installer.verify = Mock()
        self.installer.prepare_portaudio = Mock(return_value=self.installer.native_build_env(prefix))
        with patch.object(install.platform, "machine", return_value="arm64"), \
                patch.object(install.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            self.installer.install_environment("hearing")
        final_call = self.installer.pip.call_args
        self.assertEqual(final_call.args[2], "安装模块依赖")
        self.assertIn(f"-L{prefix}/lib", shlex.split(final_call.kwargs["env"]["LDFLAGS"]))
        self.installer.verify.assert_called_once_with("hearing")

    def test_failed_prefix_does_not_override_valid_custom_environment_at_pip(self):
        prefix = self.root / "leftover library"
        (prefix / "include").mkdir(parents=True)
        (prefix / "include" / "portaudio.h").touch()
        self.installer.env.update(CPATH="/custom/audio/include", LIBRARY_PATH="/custom/audio/lib")
        self.installer.portaudio_prefixes = Mock(return_value=(prefix,))
        self.installer.portaudio_probe = Mock(side_effect=[False, True])
        self.installer.prepare_venv = Mock(return_value=self.root / "hearing-python")
        self.installer.pip = Mock()
        self.installer.verify = Mock()
        with patch.object(install.sys, "platform", "darwin"), \
                patch.object(install.platform, "machine", return_value="arm64"), \
                patch.object(install.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            self.installer.install_environment("hearing")
        failed_env = self.installer.portaudio_probe.call_args_list[0].args[0]
        passed_env = self.installer.portaudio_probe.call_args_list[1].args[0]
        self.assertIn(f"-I{prefix}/include", shlex.split(failed_env["CFLAGS"]))
        self.assertNotIn("CFLAGS", passed_env)
        self.assertIs(self.installer.pip.call_args.kwargs["env"], passed_env)
        self.assertEqual(passed_env["CPATH"], "/custom/audio/include")
        self.assertEqual(passed_env["LIBRARY_PATH"], "/custom/audio/lib")

    def test_dry_run_does_not_prepare_native_tools(self):
        self.options.dry_run = True
        root = self.root / "preview"
        installer = install.Installer(self.options, root=root)
        installer.prepare_portaudio = Mock()
        self.assertEqual(installer.execute(), 0)
        self.assertFalse(root.exists())
        installer.prepare_portaudio.assert_not_called()


if __name__ == "__main__":
    unittest.main()
