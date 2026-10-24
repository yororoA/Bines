"""Install and supervise the host-only website browser bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import shlex
import socket
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler


ROOT = Path(__file__).resolve().parent
# Keep the Playwright daemon: the Rust 0.27 control session stalled live pages;
# current Rust releases reject our persistent-profile/domain-filter combination.
AGENT_BROWSER_VERSION = "0.19.0"
NODE_VERSION = "24.19.0"
INSTALL_ROOT = ROOT / ".tools" / "agent-browser"
AGENT_BROWSER_SOCKET_DIR = INSTALL_ROOT / "runtime"
AGENT_BROWSER_RUNTIME_HOME = INSTALL_ROOT / "home"
AGENT_BROWSER_CRASH_DIR = INSTALL_ROOT / "crashes"
AGENT_BROWSER_HOME = Path.home() / ".agent-browser"
CHROME_FOR_TESTING_ROOT = AGENT_BROWSER_HOME / "browsers"
BINES_BROWSER_SESSIONS = ("bines-xiaoheihe", "bines-douyin", "bines-bilibili", "bines-qzone")
WEBSITE_SPOOL_ROOT = ROOT / "thingking" / "memory_data" / "website_bridge"


def _agent_browser_version(command):
    try:
        result = subprocess.run(
            [*command, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode:
        return ""
    return (result.stdout or result.stderr or "").strip()


def _bundled_command(path):
    """Use the native binary directly, so a timeout cannot orphan a Node child."""
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(platform.machine())
    system = {"darwin": "darwin", "linux": "linux", "win32": "win32"}.get(sys.platform)
    native = path.resolve().parent / f"agent-browser-{system}-{architecture}{'.exe' if os.name == 'nt' else ''}"
    return [str(native if system and architecture and native.is_file() else path)]


def _compatible_agent_browser(command):
    version = _agent_browser_version(command)
    match = re.fullmatch(r"agent-browser\s+(\d+\.\d+\.\d+)", version)
    return bool(match and match.group(1) == AGENT_BROWSER_VERSION)


def ensure_browser_node():
    """The Playwright daemon and its HTTP client require a current Node runtime."""
    system = shutil.which("node")
    bundled = INSTALL_ROOT / "node_modules/node/bin" / ("node.exe" if os.name == "nt" else "node")
    candidates = [
        *([Path(system)] if system else []),
        *sorted((Path.home() / ".nvm/versions/node").glob("*/bin/node"), reverse=True),
        bundled,
    ]
    for candidate in candidates:
        try:
            result = subprocess.run(
                [str(candidate), "-p", "process.versions.node"],
                capture_output=True, text=True, timeout=5,
            )
            major = int(result.stdout.strip().split(".")[0])
            if result.returncode == 0 and major >= 22:
                return str(candidate)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("网站浏览器需要 Node.js 22 或更新版本")
    print(f"正在安装网站浏览器专用 Node.js {NODE_VERSION}…", flush=True)
    result = subprocess.run(
        [npm, "install", "--prefix", str(INSTALL_ROOT), f"node@{NODE_VERSION}",
         "--registry", os.environ.get("BINES_NPM_REGISTRY", "https://registry.npmjs.org"),
         "--no-audit", "--no-fund", "--legacy-peer-deps", "--omit=optional"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode or not bundled.is_file():
        raise RuntimeError("网站浏览器专用 Node.js 安装失败")
    return str(bundled)


def ensure_agent_browser():
    configured = os.environ.get("BINES_AGENT_BROWSER_COMMAND")
    if configured:
        command = shlex.split(configured)
        unsupported_mode = any(flag in command for flag in ("--native", "--engine"))
        if not command or not _compatible_agent_browser(command) or unsupported_mode:
            raise RuntimeError(
                f"BINES_AGENT_BROWSER_COMMAND 必须使用 agent-browser {AGENT_BROWSER_VERSION}"
                " 的 Playwright 模式（持久登录目录与域名限制）"
            )
        return command

    bundled = INSTALL_ROOT / "node_modules" / ".bin" / (
        "agent-browser.cmd" if os.name == "nt" else "agent-browser"
    )
    if bundled.is_file() and _compatible_agent_browser(_bundled_command(bundled)):
        return _bundled_command(bundled)

    system = shutil.which("agent-browser")
    if system and _compatible_agent_browser([system]):
        return [system]

    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("缺少 npm，无法安装网站浏览器运行时")
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    registry = os.environ.get("BINES_NPM_REGISTRY", "https://registry.npmjs.org")
    result = subprocess.run(
        [
            npm, "install", "--prefix", str(INSTALL_ROOT),
            f"agent-browser@{AGENT_BROWSER_VERSION}",
            "--registry", registry, "--no-audit", "--no-fund",
            "--legacy-peer-deps", "--omit=optional",
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode or not bundled.is_file():
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError("安装 agent-browser 失败：" + " ".join(detail))
    if not _compatible_agent_browser(_bundled_command(bundled)):
        raise RuntimeError("agent-browser 安装后无法执行")
    return _bundled_command(bundled)


def _version_key(path):
    value = path.name.removeprefix("chrome-")
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


def _chrome_for_testing_binary(install_dir):
    install_dir = Path(install_dir)
    if sys.platform == "darwin":
        relative = Path(
            "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
        return install_dir / relative
    if os.name == "nt":
        candidates = (
            install_dir / "chrome.exe",
            install_dir / "chrome-win64" / "chrome.exe",
        )
    else:
        candidates = (
            install_dir / "chrome",
            install_dir / "chrome-linux64" / "chrome",
        )
    return next((path for path in candidates if path.is_file()), candidates[0])


def installed_chrome_for_testing(root=CHROME_FOR_TESTING_ROOT):
    root = Path(root).expanduser()
    installs = sorted(
        (path for path in root.glob("chrome-*") if path.is_dir()),
        key=_version_key,
        reverse=True,
    )
    for install in installs:
        binary = _chrome_for_testing_binary(install)
        if binary.is_file():
            return str(binary)
    return ""


def _is_chrome_for_testing(path):
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "Chrome for Testing" in (
        (result.stdout or "") + (result.stderr or "")
    )


def ensure_chrome_for_testing(command):
    configured = os.environ.get("BINES_CHROME_FOR_TESTING_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file() or not _is_chrome_for_testing(path):
            raise RuntimeError(
                "BINES_CHROME_FOR_TESTING_EXECUTABLE 必须指向 Chrome for Testing"
            )
        return str(path)
    installed = installed_chrome_for_testing()
    if installed:
        return installed
    print("首次运行正在安装 Chrome for Testing（约 200 MB）…", flush=True)
    try:
        result = subprocess.run(
            [*command, "install"],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("安装 Chrome for Testing 超时") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError("安装 Chrome for Testing 失败：" + " ".join(detail))
    installed = installed_chrome_for_testing()
    if not installed:
        raise RuntimeError("Chrome for Testing 安装完成但未找到可执行文件")
    return installed


def _active_bines_sessions(command, env=None):
    try:
        result = subprocess.run(
            [*command, "session", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() in BINES_BROWSER_SESSIONS
    }


def close_stale_bines_sessions(command, env=None):
    active = _active_bines_sessions(command, env)
    if active is None:
        raise RuntimeError("无法读取 Bines 浏览器会话，拒绝回退到原 Chrome")
    for session in active:
        try:
            subprocess.run(
                [*command, "--session", session, "close"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    deadline = time.monotonic() + 5
    while active and time.monotonic() < deadline:
        active = _active_bines_sessions(command, env)
        if active is None:
            raise RuntimeError("无法确认旧的 Bines 浏览器会话已退出")
        if active:
            time.sleep(0.1)
    if active:
        raise RuntimeError("旧的 Bines 浏览器会话未退出，拒绝回退到原 Chrome")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebsiteBridgeProcess:
    def __init__(self):
        self.process = None
        self.log = None
        self.token = ""
        self.port = 0
        self.owner_lock = None

    def start(self):
        if os.environ.get("BINES_WEBSITE_ENABLED", "true").lower() in {"0", "false", "no"}:
            return False
        from common.website_spool import acquire_bridge_lock
        self.owner_lock = acquire_bridge_lock(WEBSITE_SPOOL_ROOT / ".runtime.lock")
        try:
            return self._start()
        except BaseException:
            self.close()
            raise

    def _start(self):
        command = ensure_agent_browser()
        node = ensure_browser_node()
        chrome = ensure_chrome_for_testing(command)
        for directory in (
            AGENT_BROWSER_SOCKET_DIR,
            AGENT_BROWSER_RUNTIME_HOME,
            AGENT_BROWSER_CRASH_DIR,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        required_args = [
            "--disable-breakpad",
            "--disable-crash-reporter",
            "--no-first-run",
            "--no-default-browser-check",
            f"--crash-dumps-dir={AGENT_BROWSER_CRASH_DIR}",
        ]
        configured_args = os.environ.get("AGENT_BROWSER_ARGS", "").replace("\n", ",").split(",")
        browser_args = list(dict.fromkeys(
            [*required_args, *(item.strip() for item in configured_args if item.strip())]
        ))
        browser_env = {
            **os.environ,
            "AGENT_BROWSER_ARGS": ",".join(browser_args),
            "AGENT_BROWSER_SOCKET_DIR": str(AGENT_BROWSER_SOCKET_DIR),
            "AGENT_BROWSER_NATIVE": "0",
            "HOME": str(AGENT_BROWSER_RUNTIME_HOME),
            "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", ""),
        }
        browser_env.pop("AGENT_BROWSER_ENGINE", None)
        close_stale_bines_sessions(command, browser_env)
        self.token, self.port = secrets.token_hex(32), _free_port()
        logs = ROOT / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.log = (logs / "website-bridge.log").open("a", encoding="utf-8")
        env = {
            **browser_env,
            "BINES_AGENT_BROWSER_COMMAND": shlex.join(command),
            "BINES_CHROME_FOR_TESTING_EXECUTABLE": chrome,
        }
        self.process = subprocess.Popen(
            [
                sys.executable, str(ROOT / "website_bridge.py"),
                "--host", "127.0.0.1", "--port", str(self.port), f"--token={self.token}",
                "--spool", str(WEBSITE_SPOOL_ROOT),
            ],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        host_url = f"http://127.0.0.1:{self.port}"
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 20
        last_error = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                last_error = f"进程退出（代码 {self.process.returncode}）"
                break
            try:
                request = Request(
                    host_url + "/health",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                with opener.open(request, timeout=1) as response:
                    payload = json.load(response)
                if payload.get("status") == "ok":
                    os.environ["BINES_WEBSITE_BRIDGE_HOST_URL"] = host_url
                    os.environ["BINES_WEBSITE_BRIDGE_URL"] = host_url
                    os.environ["BINES_WEBSITE_BRIDGE_TOKEN"] = self.token
                    return True
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.2)
        self.close()
        raise RuntimeError("网站浏览器桥启动失败：" + (last_error or "等待就绪超时"))

    def close(self):
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self.log:
            self.log.close()
            self.log = None
        if self.owner_lock:
            self.owner_lock.close()
            self.owner_lock = None
        for key in (
            "BINES_WEBSITE_BRIDGE_HOST_URL",
            "BINES_WEBSITE_BRIDGE_URL",
            "BINES_WEBSITE_BRIDGE_TOKEN",
        ):
            os.environ.pop(key, None)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
