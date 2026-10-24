"""One-command installation of Bines' isolated runtime environments (stdlib only)."""
from __future__ import annotations

import argparse
import ctypes.util
import getpass
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from runtime_environments import ENVIRONMENTS, ROOT, environment_python

DEFAULT_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEFAULT_PYTHON_MIRROR = (
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
)
DOCKER_DESKTOP_MAC_URLS = {
    "arm64": "https://desktop.docker.com/mac/main/arm64/Docker.dmg",
    "aarch64": "https://desktop.docker.com/mac/main/arm64/Docker.dmg",
    "x86_64": "https://desktop.docker.com/mac/main/amd64/Docker.dmg",
    "amd64": "https://desktop.docker.com/mac/main/amd64/Docker.dmg",
}
PROFILES = {"chatbot": ("launcher",), "full": tuple(ENVIRONMENTS)}
IMPORTS = {
    "launcher": ("flask", "flask_cors", "zmq", "requests", "websocket", "openai", "yaml", "mcp"),
    "server": ("PyQt6.QtCore", "PyQt6.QtWidgets", "zmq", "requests", "PIL"),
    # PyAutoGUI opens a display at import time: check its distribution separately.
    "thinking": ("flask", "flask_cors", "zmq", "websocket", "openai", "mcp", "PIL", "numpy",
                 "cv2", "mss", "imagehash", "pytesseract", "chromadb", "langchain_huggingface",
                 "sentence_transformers", "torch"),
    "visual": ("cv2", "numpy", "PIL", "zmq", "requests", "insightface", "onnxruntime"),
    "hearing": ("funasr", "pyaudio", "numpy", "zmq", "librosa", "resemblyzer", "torch", "torchaudio"),
    "singing": ("audio_separator.separator", "pedalboard", "pyloudnorm", "soundfile", "imageio_ffmpeg",
                "torch", "torchaudio", "torchvision", "onnxruntime"),
}
ML_ENVIRONMENTS = {"thinking", "hearing", "singing"}
PROJECT_IMPORTS = {
    "launcher": ("start_modules", "chatBot.main", "thingking.qq_thinking",
                 "chatBot.memory_hub", "memory.lexical_index", "memory.worker",
                 "persona_profile", "prompt_builder", "agents", "mcp_runtime", "ark_mcp_client"),
    "thinking": ("memory.server", "memory.embedding", "memory.vector_index", "thingking.rag_server"),
}


class InstallError(RuntimeError):
    pass


@contextmanager
def installation_lock(root):
    """Serialize installs without deleting or rebuilding an environment in use."""
    directory = Path(root) / ".tools"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "install.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise InstallError("已有依赖安装任务正在运行") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise InstallError("已有依赖安装任务正在运行") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def clean_environment():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PIP_", "UV_"))
           and key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"}}
    env.update(PYTHONNOUSERSITE="1", PYTHONUNBUFFERED="1", PYTHONUTF8="1",
               PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull)
    return env


def python_info(command, env=None):
    try:
        result = subprocess.run([*command, "-I", "-c", (
            "import json,sys,struct; print(json.dumps({'version':list(sys.version_info[:2]),"
            "'bits':str(struct.calcsize('P')*8)+'bit','prefix':sys.prefix,'base_prefix':sys.base_prefix,"
            "'executable':sys.executable}))"
        )], capture_output=True, text=True, timeout=15, env=env or clean_environment())
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def supported_python(info):
    return info and info["version"] in ([3, 11], [3, 12]) and info["bits"] == "64bit"


def selected_environments(profile, only=None):
    selected = set(only or PROFILES[profile])
    selected.add("launcher")
    return [name for name in ENVIRONMENTS if name in selected]


class Installer:
    def __init__(self, options, root=ROOT):
        self.options = options
        self.root = Path(root)
        self.selected = selected_environments(options.profile, options.only)
        self.cache_root = (Path(options.cache_dir).expanduser().resolve()
                           if getattr(options, "cache_dir", None)
                           else self.root / ".tools" / "cache")
        if not options.dry_run:
            self.cache_root.mkdir(parents=True, exist_ok=True)
            logs = self.cache_root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            self.log_dir = Path(tempfile.mkdtemp(prefix="install-", dir=logs))
        else:
            self.log_dir = None
        self.results = {}
        self.docker_install_error = None
        self.command_number = 0
        self.env = clean_environment()
        self.env["UV_PYTHON_INSTALL_DIR"] = str(self.root / ".tools" / "python")
        # Keep pip/uv caches, build temp files and model downloads out of the system
        # drive: Windows users would otherwise get several GB in %LOCALAPPDATA%.
        if not options.dry_run:
            for directory in ("pip", "uv", "hf", "tmp"):
                (self.cache_root / directory).mkdir(parents=True, exist_ok=True)
        self.env.update(
            PIP_CACHE_DIR=str(self.cache_root / "pip"),
            UV_CACHE_DIR=str(self.cache_root / "uv"),
            HF_HOME=str(self.cache_root / "hf"),
            TMPDIR=str(self.cache_root / "tmp"),
            TEMP=str(self.cache_root / "tmp"),
            TMP=str(self.cache_root / "tmp"),
        )
        self.device = self.select_device(options.device)
        self.python = None
        self.docker_build_error = None

    @staticmethod
    def select_device(requested):
        machine = platform.machine().lower()
        if requested == "cuda" and (sys.platform not in {"win32", "linux"} or machine not in {"amd64", "x86_64"}):
            raise InstallError("CUDA 安装仅支持 Windows/Linux x86_64；此机器请使用 --device cpu")
        return requested

    def run(self, command, label, *, env=None, timeout=1800, live_progress=False):
        """Keep pip/compiler noise in a log and print one heartbeat every 20 seconds."""
        self.command_number += 1
        log = self.log_dir / f"{self.command_number:02d}.log"
        print(f"  {label}…", flush=True)
        if live_progress:
            from common.docker_progress import stream_build
            result = stream_build(
                [str(part) for part in command], cwd=self.root, env=env or self.env,
                log_path=log, timeout=timeout,
            )
            if result.returncode:
                raise InstallError(f"{label}失败（退出码 {result.returncode}），日志：{log}")
            return log
        with log.open("w", encoding="utf-8") as stream:
            from process_runtime import ProcessTree
            tree = ProcessTree([str(part) for part in command], cwd=self.root,
                                       env=env or self.env, stdout=stream, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL)
            process = tree.process
            started, next_notice = time.monotonic(), time.monotonic()+20
            try:
                while process.poll() is None:
                    now = time.monotonic()
                    if now-started > timeout:
                        raise InstallError(f"{label}超时，日志：{log}")
                    if now >= next_notice:
                        print(f"  {label}进行中（{int(now-started)} 秒）", flush=True)
                        next_notice = now+20
                    time.sleep(.2)
            except BaseException:
                tree.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tree.kill()
                    process.wait()
                raise
            finally:
                tree.kill()
                tree.close()
        if process.returncode:
            raise InstallError(f"{label}失败（退出码 {process.returncode}），日志：{log}")
        return log

    def choose_python(self):
        if self.options.python:
            resolved = shutil.which(self.options.python) or str(Path(self.options.python).expanduser())
            info = python_info([resolved], self.env)
            if not supported_python(info):
                raise InstallError("--python 必须指向 64 位 Python 3.11 或 3.12")
            return info["executable"]
        candidates = []
        for version in ("3.12", "3.11"):
            found = shutil.which("python"+version)
            if found:
                candidates.append([found])
            if os.name == "nt" and shutil.which("py"):
                candidates.append(["py", "-"+version])
        candidates.append([sys.executable])
        candidates.extend([str(environment_python(name, self.root))] for name in ENVIRONMENTS
                          if environment_python(name, self.root).is_file())
        managed = self.root / ".tools" / "python"
        for pattern in ("*/bin/python3.12", "*/bin/python3.11", "*/python.exe"):
            candidates.extend([str(path)] for path in managed.glob(pattern))
        for command in candidates:
            info = python_info(command, self.env)
            if supported_python(info):
                return info["executable"]
        # UV_PYTHON_INSTALL_DIR above isolates new downloads, but also hides uv's
        # existing user cache. Discover that cache separately, strictly offline.
        uv = shutil.which("uv")
        if uv:
            discovery_env = clean_environment()
            existing_dir = os.environ.get("UV_PYTHON_INSTALL_DIR")
            if existing_dir:
                discovery_env["UV_PYTHON_INSTALL_DIR"] = existing_dir
            for version in ("3.12", "3.11"):
                try:
                    found = subprocess.run(
                        [uv, "--no-config", "--offline", "python", "find", "--no-project",
                         "--managed-python", "--no-python-downloads", version],
                        env=discovery_env, capture_output=True, text=True, timeout=10,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                candidate = found.stdout.strip()
                info = python_info([candidate], self.env) if found.returncode == 0 else None
                if supported_python(info):
                    return info["executable"]
        if self.options.no_python_download:
            raise InstallError("未找到 64 位 Python 3.11/3.12；请安装或使用 --python 指定")
        # Download Python into this project; never replace the user's system Python.
        if not uv:
            bootstrap = self.root / ".tools" / "installer"
            bootstrap_python = bootstrap / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not bootstrap_python.exists():
                self.run([sys.executable, "-m", "venv", bootstrap], "创建安装器环境")
            self.pip(bootstrap_python, ["uv>=0.12,<1"], "安装 Python 下载工具")
            uv = str(bootstrap_python.parent / ("uv.exe" if os.name == "nt" else "uv"))
        command = [uv, "--no-config", "python", "install", "--no-bin", "--no-registry", "3.12"]
        if self.options.python_mirror:
            command += ["--mirror", self.options.python_mirror]
        download_env = {**self.env, "UV_HTTP_CONNECT_TIMEOUT": "15",
                        "UV_HTTP_TIMEOUT": "45", "UV_HTTP_RETRIES": "1"}
        try:
            self.run(command, "下载项目专用 Python 3.12", env=download_env,
                     timeout=self.options.python_download_timeout)
        except InstallError as exc:
            raise InstallError(
                f"{exc}\nPython 下载尚未完成；可用 --python 指定已有 Python 3.12，"
                "或 --python-mirror 更换公开镜像。"
                "\n--index-url 只影响 pip 包，不影响 Python 安装包下载。"
            ) from exc
        found = subprocess.run([uv, "--no-config", "python", "find", "--managed-python", "3.12"],
                               env=self.env, capture_output=True, text=True, timeout=15)
        candidate = found.stdout.strip()
        if found.returncode or not supported_python(python_info([candidate])):
            raise InstallError("未找到下载的 Python 3.12，可用 --python 指定已有解释器")
        return candidate

    def pip(self, python, packages, label, *, extra=(), env=None):
        command = [python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                   "install", "--no-input", "--progress-bar", "off", "--prefer-binary",
                   "--retries", "2", "--timeout", "60",
                   "--index-url", self.options.index_url]
        command += list(extra) + list(packages)
        return self.run(command, label, env=env)

    def system_packages(self):
        """Opt-in system package installation; normal Python install never escalates."""
        heavy = set(self.selected) - {"launcher"}
        if not self.options.system or not heavy:
            return
        if sys.platform == "darwin":
            if not shutil.which("brew"):
                raise InstallError("--system 需要已安装 Homebrew；安装后重试，或自行准备系统依赖")
            packages = ["pkg-config"]
            if heavy & {"server", "singing"}:
                packages.append("ffmpeg")
            if "hearing" in heavy:
                packages.append("portaudio")
            if "thinking" in heavy:
                packages.extend(["tesseract", "tesseract-lang"])
            if "singing" in heavy:
                packages.extend(["cmake", "libsamplerate"])
            self.run(["brew", "install", *packages], "安装系统音视频依赖")
        elif sys.platform == "linux" and shutil.which("apt-get"):
            prefix = []
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                if not shutil.which("sudo") or not sys.stdin.isatty():
                    raise InstallError("--system 的 apt 安装需要 sudo；请在交互终端重试，或按 docs/INSTALL.md 手动安装系统包")
                subprocess.run(["sudo", "-v"], check=True)
                prefix = ["sudo", "-n"]
            self.run([*prefix, "apt-get", "update"], "刷新系统软件索引")
            self.run([*prefix, "apt-get", "install", "-y", "build-essential", "pkg-config", "cmake",
                      "portaudio19-dev", "libsamplerate0-dev", "libsndfile1", "ffmpeg",
                      "tesseract-ocr", "tesseract-ocr-chi-sim", "libgl1", "libxcb-cursor0"],
                     "安装系统音视频依赖")
        elif os.name == "nt" and shutil.which("winget"):
            for package in ("Gyan.FFmpeg", "UB-Mannheim.TesseractOCR"):
                self.run(["winget", "install", "--id", package, "-e", "--silent",
                          "--accept-source-agreements", "--accept-package-agreements"],
                         "安装 "+package)
        else:
            raise InstallError("当前平台没有可用的系统包管理器；请按 docs/INSTALL.md 准备系统依赖")

    def install_docker_desktop(self):
        """Install Docker Desktop from Docker's public installer without accepting its license."""
        import napcat

        docker = napcat.docker_executable()
        if docker:
            print(f"  复用 Docker：{docker}", flush=True)
            return
        if sys.platform == "darwin":
            machine = platform.machine().lower()
            url = DOCKER_DESKTOP_MAC_URLS.get(machine)
            if not url:
                raise InstallError(f"Docker Desktop 不支持当前 Mac 架构：{machine}")
            download_dir = self.root / ".tools" / "downloads"
            download_dir.mkdir(parents=True, exist_ok=True)
            dmg = download_dir / "Docker.dmg"
            partial = download_dir / "Docker.dmg.part"
            if not dmg.is_file():
                curl = shutil.which("curl") or "/usr/bin/curl"
                self.run(
                    [curl, "--fail", "--location", "--retry", "2", "--connect-timeout", "15",
                     "--continue-at", "-", "--output", partial, url],
                    "下载 Docker Desktop（约 600 MB）",
                    timeout=self.options.docker_download_timeout,
                )
                partial.replace(dmg)
            else:
                print("  复用已下载的 Docker Desktop 安装包", flush=True)

            mount = self.log_dir / "docker-volume"
            mount.mkdir()
            try:
                self.run(
                    ["/usr/bin/hdiutil", "attach", dmg, "-nobrowse", "-readonly",
                     "-mountpoint", mount],
                    "挂载 Docker Desktop 安装包",
                    timeout=120,
                )
            except Exception:
                dmg.unlink(missing_ok=True)
                raise
            app = mount / "Docker.app"
            installer = app / "Contents" / "MacOS" / "install"
            try:
                if not installer.is_file():
                    raise InstallError("Docker Desktop 安装包中缺少官方安装程序")
                try:
                    self.run(
                        ["/usr/sbin/spctl", "--assess", "--type", "execute",
                         "--verbose=2", app],
                        "验证 Docker Desktop 签名",
                        timeout=120,
                    )
                except Exception:
                    dmg.unlink(missing_ok=True)
                    raise
                elevated = shlex.join([str(installer), f"--user={getpass.getuser()}"])
                script = f"do shell script {json.dumps(elevated)} with administrator privileges"
                print("  macOS 将请求一次管理员授权；Docker 协议仍由你在首次启动窗口确认。",
                      flush=True)
                self.run(
                    ["/usr/bin/osascript", "-e", script],
                    "安装 Docker Desktop",
                    timeout=900,
                )
            finally:
                subprocess.run(
                    ["/usr/bin/hdiutil", "detach", mount, "-force"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                )
            if not napcat.docker_executable():
                raise InstallError("Docker Desktop 安装结束，但未找到 Docker CLI")
            dmg.unlink(missing_ok=True)
            napcat.start_docker_desktop()
            print("  Docker Desktop 已打开；首次使用请在其窗口接受协议并完成初始化。",
                  flush=True)
            return

        if os.name == "nt":
            winget = shutil.which("winget")
            if not winget:
                raise InstallError("Windows 自动安装 Docker Desktop 需要 winget")
            print("  Docker Desktop 安装到 C:\\Program Files\\Docker，镜像数据也在 C 盘；"
                  "不想占用 C 盘可加 --skip-docker，改用已有的 Docker 或外部 NapCat。", flush=True)
            self.run(
                [winget, "install", "--id", "Docker.DockerDesktop", "--exact", "--silent",
                 "--accept-source-agreements", "--accept-package-agreements"],
                "安装 Docker Desktop",
                timeout=self.options.docker_download_timeout,
            )
            napcat.start_docker_desktop()
            return
        raise InstallError("Linux 暂不自动修改系统 Docker 仓库；请安装 Docker Engine 或使用外部 NapCat")

    def prepare_docker(self):
        if self.options.skip_docker:
            return
        try:
            self.install_docker_desktop()
        except (InstallError, OSError, subprocess.SubprocessError) as exc:
            self.docker_install_error = str(exc)
            print(f"  Docker 自动安装未完成：{exc}", flush=True)
            print("  Python 依赖将继续安装；之后可重新运行，或使用 --skip-docker 跳过。",
                  flush=True)

    def native_build_env(self):
        env = dict(self.env)
        if sys.platform == "darwin":
            # PyAudio's sdist needs Homebrew's PortAudio headers and linker path.
            for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
                if (prefix / "include" / "portaudio.h").exists():
                    env["CFLAGS"] = env.get("CFLAGS", "") + f" -I{prefix}/include"
                    env["LDFLAGS"] = env.get("LDFLAGS", "") + f" -L{prefix}/lib"
        return env

    def prepare_venv(self, name):
        python = environment_python(name, self.root)
        directory = self.root / ENVIRONMENTS[name]
        if directory.exists():
            if not (directory / "pyvenv.cfg").is_file() or not python.is_file():
                raise InstallError(f"{directory} 不是完整虚拟环境；请备份后移走，安装器不会自动删除")
            info = python_info([str(python)])
            if not supported_python(info) or Path(info["prefix"]).absolute() != directory.absolute():
                raise InstallError(f"{directory} 的 Python 不兼容；需要 64 位 3.11/3.12，请备份并移走后重试")
            print("  复用现有虚拟环境", flush=True)
        else:
            self.run([self.python, "-I", "-m", "venv", directory], "创建虚拟环境")
        self.run([python, "-I", "-m", "ensurepip", "--upgrade"], "检查 pip")
        return python

    def install_environment(self, name):
        print(f"\n[{name}] {ENVIRONMENTS[name]}", flush=True)
        if name in ML_ENVIRONMENTS and sys.platform == "darwin" and platform.machine() == "x86_64":
            raise InstallError(f"{name} 的 Torch 2.7 不提供 Intel macOS wheel；请使用 Apple Silicon/Windows/Linux，或 --profile chatbot")
        python = self.prepare_venv(name)
        self.pip(python, ["pip>=24.3,<27", "setuptools>=75,<81", "wheel"], "准备安装工具")
        build_env = self.native_build_env()
        extra = []
        packages = ["-r", str(self.root / "requirements" / f"{name}.txt")]
        if name in ML_ENVIRONMENTS or name == "visual":
            extra += ["-c", str(self.root / "requirements" / "ml-constraints.txt")]
        if name in {"visual", "hearing", "singing"}:
            # InsightFace/diffq/webrtcvad source builds need local headers/Cython.
            self.pip(python, ["numpy==1.26.4", "Cython>=0.29.36,<3", "cmake>=3.26,<4", "pybind11>=2.11,<3"],
                     "准备扩展编译依赖")
            extra += ["--no-build-isolation"]
        if name in ML_ENVIRONMENTS:
            torch_packages = ["torch==2.7.1", "torchaudio==2.7.1", "torchvision==0.22.1"]
            torch_extra = ["-c", str(self.root / "requirements" / "ml-constraints.txt")]
            if sys.platform != "darwin":
                torch_extra += ["--index-url", "https://download.pytorch.org/whl/" +
                                ("cu128" if self.device == "cuda" else "cpu")]
            installed = subprocess.run(
                [python, "-I", "-c", "import importlib.metadata as m;print(m.version('torch'))"],
                capture_output=True, text=True, env=self.env, timeout=15,
            )
            version = installed.stdout.strip()
            wrong_build = (not version.endswith("+cu128") if self.device == "cuda"
                           else "+cu" in version)
            if installed.returncode == 0 and wrong_build:
                torch_extra.append("--force-reinstall")
            self.pip(python, torch_packages, "安装配套 PyTorch", extra=torch_extra)
        if name in {"visual", "singing"}:
            # Switching device explicitly must not leave two onnxruntime providers.
            opposite = "onnxruntime" if self.device == "cuda" else "onnxruntime-gpu"
            self.run([python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                      "uninstall", "-y", opposite], "检查 ONNX Runtime 版本")
            packages += ["onnxruntime-gpu[cuda,cudnn]==1.22.0" if self.device == "cuda" else "onnxruntime==1.22.0"]
        self.pip(python, packages, "安装模块依赖", extra=extra, env=build_env)
        self.verify(name)

    def verify(self, name):
        python = environment_python(name, self.root)
        info = python_info([str(python)], self.env)
        directory = self.root / ENVIRONMENTS[name]
        if not supported_python(info) or Path(info["prefix"]).absolute() != directory.absolute():
            raise InstallError(f"环境缺少兼容的 64 位 Python 3.11/3.12：{ENVIRONMENTS[name]}")
        self.run([python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "check"],
                 "核验依赖冲突", timeout=120)
        imports = list(IMPORTS[name])
        check = (
            "import importlib,importlib.metadata as m;"
            f"[importlib.import_module(x) for x in {imports!r}];"
        )
        if name == "thinking":
            check += "assert m.version('PyAutoGUI');"
            if os.name == "nt":
                check += "[importlib.import_module(x) for x in ['win32gui','pywinauto','uiautomation']];"
        if name in ML_ENVIRONMENTS:
            check += ("import torch,torchaudio,torchvision;"
                      "assert torch.__version__.split('+')[0]=='2.7.1';"
                      "assert torchaudio.__version__.split('+')[0]=='2.7.1';"
                      "assert torchvision.__version__.split('+')[0]=='0.22.1';")
            if self.device == "cuda":
                check += ("assert torch.version.cuda=='12.8','PyTorch 不是 cu128 版本';"
                          "assert torch.cuda.is_available(),'CUDA 不可用，请检查 NVIDIA 驱动';")
            elif sys.platform != "darwin":
                check += "assert torch.version.cuda is None,'PyTorch 仍是 CUDA 版本，请重跑 CPU 安装';"
        if name in {"visual", "singing"} and self.device == "cuda":
            check += "import onnxruntime as ort;ort.preload_dlls();assert 'CUDAExecutionProvider' in ort.get_available_providers();"
        self.run([python, "-I", "-c", check], "核验核心模块导入", timeout=180)
        if name in PROJECT_IMPORTS:
            probe_dir = self.log_dir / "import-check"
            probe_dir.mkdir(exist_ok=True)
            env = {**self.env, "BINES_ENV_FILE": str(probe_dir / "unused.env"),
                   "BINES_MEMORY_DIR": str(probe_dir / "memory"),
                   "BINES_PROFILE": self.options.profile, "_BINES_LAUNCH_PROFILE": self.options.profile}
            code = ("import sys,importlib;"
                    f"sys.path[:0]={[str(self.root), str(self.root / 'thingking' / 'src')]!r};"
                    f"[importlib.import_module(x) for x in {PROJECT_IMPORTS[name]!r}];")
            if name == "launcher":
                code += ("assert 'chromadb' not in sys.modules;"
                         "assert 'torch' not in sys.modules;"
                         "assert 'pyautogui' not in sys.modules;")
            self.run([python, "-I", "-B", "-c", code], "核验合并后的 QQ/记忆模块", env=env, timeout=180)

    def initialize_settings(self):
        from common.env import read_env, env_path
        target = env_path() if self.root == ROOT else self.root / ".env"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                target.chmod(0o600)
                stream.write((self.root / ".env.example").read_text(encoding="utf-8"))
        # An explicit blank is user configuration too; only add absent keys.
        defaults = {"BINES_PROFILE": self.options.profile}
        if self.results.get("hearing") == "ok":
            defaults["HEARING_ASR_DEVICE"] = "cuda" if self.device == "cuda" else "cpu"
        if self.results.get("singing") == "ok":
            defaults["SINGING_PYTHON"] = str(environment_python("singing", self.root))
        values, text = read_env(target), target.read_text(encoding="utf-8")
        for key, value in defaults.items():
            if key in values:
                continue
            encoded = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
            line = f"{key}={encoded}"
            text = text.rstrip() + "\n" + line + "\n"
        from napcat import write_secret
        write_secret(target, text)

    def external_status(self):
        notices = []
        import napcat
        docker = napcat.docker_executable()
        if not docker:
            detail = f"（{self.docker_install_error}）" if self.docker_install_error else ""
            notices.append("Docker 未安装：QQ 容器需要 Docker Desktop/Engine，或外部 NapCat" + detail)
        else:
            try:
                ready = subprocess.run(
                    [docker, "info", "--format", "{{.ServerVersion}}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                ).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ready = False
            if not ready and napcat.docker_desktop_app():
                notices.append("Docker Desktop 已安装并已打开；首次请在其窗口接受协议并完成初始化")
        if "server" in self.selected and not shutil.which("ffplay"):
            notices.append("ffplay 未在 PATH：桌面声音播放需要系统 FFmpeg（必须含 ffplay）")
        if "thinking" in self.selected:
            executable = shutil.which("tesseract")
            if not executable:
                notices.append("Tesseract 未在 PATH：快速屏幕 OCR 需要 tesseract 与 chi_sim/eng 语言包")
            else:
                result = subprocess.run([executable, "--list-langs"], capture_output=True, text=True, timeout=10)
                if "chi_sim" not in result.stdout:
                    notices.append("Tesseract 缺少 chi_sim 中文语言包")
        if "hearing" in self.selected and os.name != "nt" and not ctypes.util.find_library("portaudio"):
            notices.append("PortAudio 未检测到：PyAudio 编译/录音需要系统 PortAudio")
        if "singing" in self.selected:
            from common.env import read_env
            values = read_env(self.root / ".env")
            if not values.get("SOVITS_SVC_DIR"):
                notices.append("唱歌换声还需配置 SOVITS_SVC_DIR 和自己的模型权重；安装器仅管理分离/混音环境")
        return notices

    def prepare_docker_images(self):
        """Build the launcher image so ``python bines.py`` works immediately."""
        if self.options.skip_docker:
            return
        if not shutil.which("docker"):
            print("  Docker 不可用，跳过镜像构建；python bines.py 将回退宿主机模式", flush=True)
            return
        from docker_launcher import (
            _environment, build_command, cleanup_build_cache, cleanup_images, ensure_builder, ensure_engine,
        )

        try:
            ensure_engine(timeout=self.options.docker_download_timeout)
        except (RuntimeError, OSError) as exc:
            self.docker_build_error = str(exc)
            print(f"  Docker 引擎未就绪，跳过镜像构建：{exc}", flush=True)
            return
        env = dict(self.env)
        docker_env = _environment(self.options.profile)
        for key in ("BINES_PROFILE", "BINES_UID", "BINES_GID", "BINES_CHATBOT_HASH", "BINES_MEMORY_SERVER_HASH",
                    "BUILDX_CONFIG"):
            env[key] = docker_env[key]
        services = ["chatbot"]
        if self.options.docker_full:
            services.append("memory-server")
        for service in services:
            try:
                ensure_builder()
                try:
                    self.run(build_command(self.options.profile, service),
                             f"构建 Docker 镜像 {service}", env=env, timeout=3600, live_progress=True)
                finally:
                    cleanup_build_cache()
                cleanup_images()
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                self.docker_build_error = str(exc)
                print(f"  Docker 镜像 {service} 构建失败：{exc}", flush=True)
                print("  可稍后运行 python bines.py，由启动器自动构建", flush=True)

    def execute(self):
        print("Bines 依赖安装 · " + self.options.profile + " · " + self.device, flush=True)
        if self.options.dry_run:
            for name in self.selected:
                print(f"  {name:10} → {ENVIRONMENTS[name]} · requirements/{name}.txt")
            if not self.options.skip_docker:
                print("  docker     → 缺失时自动安装 Docker Desktop（首次需确认协议）")
            print(f"  cache      → {self.cache_root}")
            print("仅预览；未下载或修改文件。Python 使用 64 位 3.11/3.12。")
            return 0
        print(f"详细日志：{self.log_dir}", flush=True)
        if not self.options.check:
            self.prepare_docker()
            self.system_packages()
            self.python = self.choose_python()
            print(f"使用 Python：{self.python}", flush=True)
        for name in self.selected:
            try:
                if self.options.check:
                    print(f"\n[{name}] 检查已有环境", flush=True)
                    self.verify(name)
                else:
                    self.install_environment(name)
                self.results[name] = "ok"
                print(f"  ✓ {name} 完成", flush=True)
            except (InstallError, OSError, subprocess.SubprocessError) as exc:
                self.results[name] = str(exc)
                print(f"  ✗ {name}：{exc}", flush=True)
        if not self.options.check and self.results.get("launcher") == "ok":
            self.initialize_settings()
            self.prepare_docker_images()
        print("\n安装结果：", flush=True)
        for name, result in self.results.items():
            print(f"  {name}: {'通过' if result == 'ok' else '失败'}", flush=True)
        for notice in self.external_status():
            print("  待配置：" + notice, flush=True)
        failed = [name for name, result in self.results.items() if result != "ok"]
        if failed:
            print("失败组可单独重试：python install.py --only " + " ".join(failed), flush=True)
            return 1
        if self.options.check:
            print("所选 Python 环境检查通过。", flush=True)
            return 0
        print("Python 依赖已就绪；填写 .env 的密钥后运行 python bines.py。", flush=True)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装各模块独立 Python 环境；默认完整安装，无需手动激活 venv")
    parser.add_argument("--profile", choices=PROFILES, default="full",
                        help="full=全部6组，chatbot=QQ、轻量记忆、人格与摘要")
    parser.add_argument("--only", nargs="+", choices=ENVIRONMENTS, help="仅安装/重试指定组（自动包含 launcher）")
    parser.add_argument("--python", help="已有 64 位 Python 3.11/3.12 路径；未指定时自动发现或下载")
    parser.add_argument("--no-python-download", action="store_true", help="找不到兼容 Python 时直接报告错误")
    parser.add_argument("--docker-full", action="store_true",
                        help="同时构建 RAG/thinking 镜像（体积较大，chatbot 模式也用它提供向量记忆）")
    parser.add_argument(
        "--python-mirror",
        default=os.environ.get("BINES_PYTHON_MIRROR", DEFAULT_PYTHON_MIRROR),
        help=f"Python 安装包镜像，默认南京大学镜像：{DEFAULT_PYTHON_MIRROR}",
    )
    parser.add_argument("--python-download-timeout", type=int, default=180,
                        help="Python 下载最长等待秒数，默认 180；连接和读超时另有短限制")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu", help="默认跨平台 CPU；cuda 需要 NVIDIA CUDA 12.8 兼容驱动")
    parser.add_argument("--system", action="store_true", help="额外通过已有 brew/apt/winget 安装系统音视频依赖")
    parser.add_argument("--skip-docker", action="store_true", help="不自动安装 Docker Desktop")
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("BINES_CACHE_DIR"),
        help="pip/uv 缓存、安装日志和临时文件目录；默认项目内 .tools/cache，避免占用 C 盘",
    )
    parser.add_argument("--docker-download-timeout", type=int, default=1800,
                        help="Docker Desktop 下载最长等待秒数，默认 1800")
    parser.add_argument(
        "--index-url",
        default=os.environ.get("BINES_PYPI_INDEX_URL", DEFAULT_INDEX_URL),
        help=f"Python 包镜像，默认清华 PyPI：{DEFAULT_INDEX_URL}；PyTorch CPU/CUDA 仍使用官方索引",
    )
    parser.add_argument("--check", action="store_true", help="仅检查已安装环境，不安装 Python 包")
    parser.add_argument("--dry-run", "--preview", action="store_true", help="仅预览环境与依赖清单，不修改文件")
    options = parser.parse_args(argv)
    if options.check and options.system:
        parser.error("--check 不能与 --system 同时使用")
    if options.index_url and urlsplit(options.index_url).scheme not in {"http", "https"}:
        parser.error("--index-url 必须为 HTTP(S) URL")
    if options.python_mirror and urlsplit(options.python_mirror).scheme not in {"http", "https"}:
        parser.error("--python-mirror 必须为 HTTP(S) URL")
    if options.python_download_timeout <= 0:
        parser.error("--python-download-timeout 必须大于 0")
    if options.docker_download_timeout <= 0:
        parser.error("--docker-download-timeout 必须大于 0")
    try:
        installer = Installer(options)
        if options.dry_run or options.check:
            return installer.execute()
        with installation_lock(installer.root):
            return installer.execute()
    except KeyboardInterrupt:
        print("\n安装已中止，已完成环境保留；重新执行会复用它们。")
        return 130
    except (InstallError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"安装未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    def stop_on_signal(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_signal)
    raise SystemExit(main())
