"""Docker Compose orchestration used by ``python bines.py``.

The QQ chain runs in containers; full mode still starts host-only desktop
services after the containers are healthy.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Set

ROOT = Path(__file__).resolve().parent
COMPOSE_FILE = ROOT / "deploy" / "docker" / "compose.yaml"
NAPCAT_ENV = ROOT / "deploy" / "napcat" / ".env"
BUILDER_NAME = "bines"
BUILDX_CONFIG = ROOT / ".tools" / "docker" / "buildx"
IMAGE_HASH_LABEL = "io.bines.source-hash"
BUILD_SERVICES = {"chatbot": "bines-launcher:local", "memory-server": "bines-thinking:local"}

REQUIRED_SERVICES = {
    "chatbot": ("napcat", "memory-server", "chatbot", "qq-thinking", "memory-worker", "rag-web"),
    "full": ("napcat", "memory-server", "chatbot", "qq-thinking", "memory-worker", "rag-web"),
}
HEALTH_REQUIRED = {"chatbot", "memory-server", "rag-web", "napcat", "qq-thinking", "memory-worker"}


class DockerError(RuntimeError):
    def __init__(self, message, *, details=""):
        super().__init__(message)
        self.details = details or message


def _safe_output(value) -> str:
    """Clean terminal controls and common credentials, without reading secrets."""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"(?im)^(\s*(?:Authorization|Cookie|Set-Cookie)\s*:\s*).*$", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    key = r"[\w.-]*(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|authorization|cookie|signature)[\w.-]*"
    value_pattern = r'''(?:\[REDACTED\]|"[^"\n]*"|'[^'\n]*'|[^\s,;}\]&]+)'''
    text = re.sub(r'''(?i)(["']?''' + key + r'''["']?\s*[:=]\s*)''' + value_pattern,
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(--" + key + r"\s+)" + value_pattern, r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
    return "\n".join("[环境变量快照已省略]" if re.search(
        r'''(?i)\b(?:env|environ|environment)["']?\s*[:=]\s*[\[{]''', line) or re.match(
        r"^\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}=", line) else line for line in text.splitlines())


def _error_excerpt(text):
    lines = [line for line in text.splitlines() if line.strip()]
    failures = [line for line in lines if re.search(
        r"(?i)fail|error|fatal|exception|unhealthy|timed? ?out|denied|cannot|not found|失败|错误|超时", line)]
    # Keep complete lines from both ends; never cut a container name or cause
    # halfway through a word. The private diagnostic retains the full output.
    return "\n".join(dict.fromkeys([*failures[:4], *failures[-20:], *lines[-8:]]))


def format_failure(exc, label="Docker 启动失败"):
    message = _safe_output(str(exc)).strip() or type(exc).__name__
    return message if isinstance(exc, DockerError) or message.startswith(label) else f"{label}：{message}"


def _json_rows(output):
    try:
        rows = json.loads(output)
        return rows if isinstance(rows, list) else [rows]
    except ValueError:
        return [json.loads(line) for line in output.splitlines() if line.strip()]


def _failure_log_lines(output):
    """Only infrastructure failures, never ordinary service/chat log streams."""
    selected = []
    for raw in _safe_output(output).splitlines():
        line = re.sub(r"^\S+\s+\|\s?", "", raw)
        if re.search(r'''(?i)raw_message|\b(?:content|prompt|messages?|body|args|arguments|headers|payload|user_text)["']?\s*[:=]|'''
                     r"收到.*消息|聊天|\[QQ(?:群|私聊|消息)|Received.*message", line):
            continue
        if (line.startswith("Traceback (most recent call last)") or re.match(r'^\s+File "[^"\n]+", line \d+', line)
                or re.match(r"^\s*[\w.]+(?:Error|Exception):", line)
                or re.match(r"^\s*(?:ERROR|CRITICAL|FATAL)(?:\s|:)", line)
                or re.match(r"^\d{4}-\d\d-\d\d.*\b(?:ERROR|CRITICAL|FATAL)\b", line)
                or re.match(r"^(?:memory|gateway|qq-thinking|memory-worker)[\w-]*: unhealthy\b", line)):
            selected.append(line)
    return list(dict.fromkeys(selected))[-40:]


def _startup_diagnostics(profile):
    result = run(profile, "ps", "--all", "--format", "json", timeout=15)
    if result.returncode:
        return "容器状态不可用：" + (_error_excerpt(_safe_output(result.stderr or result.stdout)) or f"退出码 {result.returncode}")
    rows = _json_rows(result.stdout)
    if not rows:
        return "未找到当前项目容器，无法收集退出码、健康检查或服务日志（容器可能已被删除）。"
    output, failed = [], []
    for row in rows:
        service = str(row.get("Service") or "")
        if service not in {*REQUIRED_SERVICES[profile], "gateway-init"}:
            continue
        state, health = str(row.get("State") or "?"), str(row.get("Health") or "-")
        output.append(f"{service}: state={state}, health={health}, exit={row.get('ExitCode', '?')}")
        if health == "unhealthy" or state in {"dead", "restarting"} or (state == "exited" and str(row.get("ExitCode")) != "0"):
            failed.append(row)
    if not failed:
        output.append("尚未发现明确失败的服务；未采集正常/等待中的服务日志。")
    for row in failed:
        service = row["Service"]
        try:
            name = str(row.get("ID") or row.get("Name") or "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                output.append(f"{service}: 容器标识缺失，无法读取退出状态与健康检查。")
            else:
                # State contains health results, not Config.Env or health command credentials.
                result = _docker("inspect", "--format", "{{json .State}}", name, timeout=10)
                if result.returncode:
                    output.append(f"{service}: 容器状态已不可用（可能已被移除）。")
                else:
                    state = json.loads(result.stdout)
                    output.append(f"{service}: exit={state.get('ExitCode', '?')}, oom_killed={state.get('OOMKilled', False)}, "
                                  + "error=" + _safe_output(state.get("Error") or "-"))
                    for entry in ((state.get("Health") or {}).get("Log") or [])[-3:]:
                        detail = "\n".join(_failure_log_lines(entry.get("Output", "")))
                        output.append(f"{service}: health_exit={entry.get('ExitCode', '?')}, "
                                      f"time={entry.get('End') or entry.get('Start') or '?'}\n"
                                      + (detail or "[健康输出无可安全展示的错误行]"))
            logs = run(profile, "logs", "--tail", "80", "--no-color", service, timeout=10)
            if logs.returncode:
                output.append(f"{service}: 服务日志不可用（命令退出码 {logs.returncode}）。")
                continue
            lines = _failure_log_lines((logs.stdout or "") + "\n" + (logs.stderr or ""))
            output.append(f"{service}: 最近 80 行日志中的错误/栈位置（普通输出已排除）\n"
                          + ("\n".join(lines) or "[无可安全展示的错误行]"))
        except Exception as exc:
            output.append(f"{service}: 诊断不可用（{type(exc).__name__}），原始启动错误保留。")
    return _safe_output("\n".join(output))


def startup_failure_report(profile, exc, *, inspect_services=True):
    """Persist safe diagnostics before the caller removes this startup's containers."""
    summary = format_failure(exc)
    try:
        diagnostics = _startup_diagnostics(profile) if inspect_services else "尚未进入容器启动阶段，未读取服务日志。"
    except Exception as diagnostic_error:
        diagnostics = f"容器诊断不可用（{type(diagnostic_error).__name__}），原始错误保留。"
    detail = _safe_output(getattr(exc, "details", str(exc)))
    try:
        directory = ROOT / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"docker-startup-{time.time_ns()}.log"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as log:
            log.write(summary + "\n\n完整命令错误输出（凭据已过滤）：\n" + detail + "\n\n" + diagnostics + "\n")
        return summary + f"\n启动诊断（已过滤凭据与普通对话输出）：{path}\n" + diagnostics
    except OSError as write_error:
        return summary + f"\n诊断文件保存失败（{type(write_error).__name__}）。\n" + diagnostics


def docker_executable() -> str | None:
    # Reuse napcat's lookup so a freshly installed Docker Desktop is found even
    # when the current shell PATH has not been refreshed yet.
    import napcat

    return napcat.docker_executable()


def ensure_engine(timeout: float = 180) -> str:
    """Start Docker Desktop when needed and wait for the daemon."""
    import napcat

    return napcat.ensure_docker_ready(timeout=int(timeout))


def available() -> bool:
    return bool(docker_executable() and COMPOSE_FILE.is_file())


def compose_command(profile: str, *args: str) -> List[str]:
    command = [docker_executable() or "docker", "compose", "-f", str(COMPOSE_FILE)]
    if NAPCAT_ENV.is_file():
        command[2:2] = ["--env-file", str(NAPCAT_ENV)]
    if profile == "full":
        command += ["--profile", "full"]
    command += list(args)
    return command


def _environment(profile: str) -> Dict[str, str]:
    from common.clock import timezone
    from common.website_spool import read_bridge_token
    env = dict(os.environ)
    env["BINES_PROFILE"] = profile
    env["BINES_MEMORY_TIMEZONE"] = timezone().key
    env["BUILDX_CONFIG"] = str(BUILDX_CONFIG)
    env.setdefault("BINES_UID", str(os.getuid() if hasattr(os, "getuid") else 1000))
    env.setdefault("BINES_GID", str(os.getgid() if hasattr(os, "getgid") else 1000))
    try:
        token = read_bridge_token(
            ROOT / "thingking" / "memory_data" / "website_bridge",
            env.get("BINES_WEBSITE_BRIDGE_TOKEN", ""),
        )
        if token:
            env["BINES_WEBSITE_BRIDGE_TOKEN"] = token
    except (OSError, ValueError):
        # A broken optional browser bridge must not block Docker maintenance.
        pass
    for service, digest in source_hashes(env).items():
        env["BINES_" + service.upper().replace("-", "_") + "_HASH"] = digest
    return env


def _process_options() -> Dict:
    # Windows 的 Ctrl+C 不应同时终止负责删除容器的 Docker 子进程。
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def run(profile: str, *args: str, timeout: float = 3600) -> subprocess.CompletedProcess:
    command = compose_command(profile, *args)
    return subprocess.run(
        command, cwd=ROOT, env=_environment(profile), text=True,
        capture_output=True, timeout=timeout, **_process_options(),
    )

def run_build(profile: str, *services: str, timeout: float = 3600):
    from common.docker_progress import stream_build
    log_path = ROOT / "logs" / "docker-build.log"
    try:
        return stream_build(
            build_command(profile, *services), cwd=ROOT, env=_environment(profile),
            log_path=log_path, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f"Docker 构建超过 {int(timeout)} 秒，日志：{log_path}") from exc


# 每个镜像分别计算内容哈希，QQ 网关独有代码变化不再触发 RAG 镜像重建。
_SHARED_SOURCES = (
    "*.py", "common/**/*.py", "tools/**/*.py",
    "thingking/*.py", "thingking/src/**/*.py", "thingking/scripts/*.py", "thingking/persona/*.yaml",
    "skills/**/*.md", "server/skill_actions.json", ".dockerignore",
    "deploy/docker/install_apt.py",
)
IMAGE_SOURCES = {
    "bines-launcher:local": _SHARED_SOURCES + (
        "chatBot/**/*.py", "chatBot/api/*.json",
        "server/*.py", "server/templates/*",
        "requirements/launcher.txt", "requirements-chatbot.txt",
        "thingking/rag_web_static/*",
        "deploy/docker/Dockerfile.launcher",
    ),
    "bines-thinking:local": _SHARED_SOURCES + (
        # RAG Server 初始化时也使用网关数据库迁移和消息身份定义。
        "chatBot/__init__.py", "chatBot/store.py", "chatBot/messages.py", "chatBot/config.py",
        "thingking/rag_web_static/*",
        "requirements/thinking.txt", "requirements/launcher.txt",
        "requirements-chatbot.txt", "requirements/ml-constraints.txt",
        "deploy/docker/Dockerfile.thinking",
    ),
}
IMAGE_HASH_EXCLUDES = frozenset({
    "common/website.py",
    "common/website_cdp.py",
    "common/website_observer.py",
    "common/website_watch.py",
    "common/visual_change.py",
    "common/docker_progress.py",
    "docker_launcher.py",
    "install.py",
    "website_bridge.py",
    "website_runtime.py",
})
IMAGE_HASH_EXCLUDES_BY_IMAGE = {
    "bines-thinking:local": frozenset({
        "common/website_spool.py",
        "thingking/qq_thinking.py",
        "skills/website/SKILL.md",
    }),
}


def source_hashes(env=None) -> Dict[str, str]:
    """按源码内容和构建参数判断镜像版本，避免 Windows 文件时间引起重复构建。"""
    env = os.environ if env is None else env
    common = hashlib.sha256()
    for key, default in (
        ("BINES_UID", str(os.getuid() if hasattr(os, "getuid") else 1000)),
        ("BINES_GID", str(os.getgid() if hasattr(os, "getgid") else 1000)),
        ("BINES_PYTHON_IMAGE", "docker.m.daocloud.io/library/python:3.12-slim"),
        ("BINES_APT_MIRROR", "https://mirrors.tuna.tsinghua.edu.cn"),
    ):
        common.update((key + "=" + env.get(key, default)).encode() + b"\0")
    hashes = {}
    for service, image in BUILD_SERVICES.items():
        digest = common.copy()
        excluded = IMAGE_HASH_EXCLUDES | IMAGE_HASH_EXCLUDES_BY_IMAGE.get(image, frozenset())
        paths = {
            path
            for pattern in IMAGE_SOURCES[image]
            for path in ROOT.glob(pattern)
            if path.is_file() and path.relative_to(ROOT).as_posix() not in excluded
        }
        for path in sorted(paths):
            content = path.read_bytes()
            digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
            digest.update(len(content).to_bytes(8, "big") + content)
        hashes[service] = digest.hexdigest()
    return hashes


def _docker(*args: str, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [docker_executable() or "docker", *args], cwd=ROOT, text=True,
        env={**os.environ, "BUILDX_CONFIG": str(BUILDX_CONFIG)},
        capture_output=True, timeout=timeout, **_process_options(),
    )


def _check_result(result, label):
    if result.returncode:
        detail = "\n".join(_safe_output(value) for value in (result.stdout, result.stderr) if value)
        summary = _error_excerpt(detail) or f"命令退出码 {result.returncode}，没有错误输出"
        raise DockerError(label + "：\n" + summary, details=detail)


def _stale_services() -> List[str]:
    expected = source_hashes()
    stale = []
    for service, image in BUILD_SERVICES.items():
        result = _docker("image", "inspect", image, timeout=30)
        try:
            labels = json.loads(result.stdout)[0]["Config"].get("Labels") or {}
        except (ValueError, IndexError, KeyError):
            labels = {}
        if result.returncode or labels.get(IMAGE_HASH_LABEL) != expected[service]:
            stale.append(service)
    return stale


def build_command(profile: str, *services: str) -> List[str]:
    return compose_command(profile, "--progress", "plain", "build", "--builder", BUILDER_NAME, *services)


def _builder_driver():
    result = _docker("buildx", "inspect", BUILDER_NAME, timeout=30)
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "Driver":
            return value.strip()
    return ""


def ensure_builder() -> None:
    """Bines 使用独立构建器，才能释放自身缓存而不清理其他项目。"""
    driver = _builder_driver()
    if driver is None:
        result = _docker(
            "buildx", "create", "--name", BUILDER_NAME, "--driver", "docker-container",
            "--driver-opt", "image=docker.m.daocloud.io/moby/buildkit:buildx-stable-1",
        )
        _check_result(result, "创建 Bines 构建器失败")
    elif driver != "docker-container":
        raise DockerError("名为 bines 的构建器不是独立构建器，无法安全清理其缓存")


def _cleanup_preserving_failure(cleanup, failure) -> None:
    """Report secondary maintenance errors without replacing a failed build."""
    try:
        cleanup()
    except Exception as exc:
        if failure is None:
            raise
        note = "附加构建清理失败（保留原始错误）：" + _safe_output(str(exc))
        if isinstance(failure, DockerError):
            failure.details += "\n" + note
        try:
            print(note, file=sys.stderr, flush=True)
        except OSError:
            pass  # A closed diagnostic stream must not replace the build error.


def cleanup_build_cache(*, prune=True) -> None:
    driver = _builder_driver()
    if driver is None:
        return
    if driver != "docker-container":
        raise DockerError("跳过共享构建器 bines 的缓存清理")
    # Removing the builder discarded unchanged apt/pip layers on every run.
    # Keep a bounded private cache; stopping the builder releases its process.
    # Shutdown only stops it, so it need not boot just to prune the same cache.
    failure = None
    try:
        if prune:
            result = _docker("buildx", "prune", "--builder", BUILDER_NAME, "--force",
                             "--max-used-space", "4GB", timeout=180)
            if result.returncode and "unknown flag: --max-used-space" in (
                    (result.stderr or "") + "\n" + (result.stdout or "")):
                # Older Buildx releases use --keep-storage for this target.
                result = _docker("buildx", "prune", "--builder", BUILDER_NAME, "--force",
                                 "--keep-storage", "4GB", timeout=180)
            _check_result(result, "回收 Bines 构建缓存失败")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        _cleanup_preserving_failure(lambda: _check_result(
            _docker("buildx", "stop", BUILDER_NAME, timeout=60), "停止 Bines 构建器失败"), failure)


def cleanup_containers() -> None:
    for project in ("bines", "bines-napcat"):
        _check_result(
            _docker("container", "prune", "--force", "--filter", f"label=com.docker.compose.project={project}"),
            "清理 Bines 已停止容器失败",
        )


def cleanup_images() -> None:
    """只删除 Compose 标记为 Bines 的无标签旧镜像；当前镜像及卷保留。"""
    _check_result(
        _docker("image", "prune", "--force", "--filter", "label=com.docker.compose.project=bines"),
        "清理 Bines 旧镜像失败",
    )


def up(profile: str) -> None:
    stale = _stale_services()
    if stale:
        print("源码有更新，正在构建：" + ", ".join(stale), flush=True)
        ensure_builder()
        failure = None
        try:
            _check_result(run_build(profile, *stale), "Docker 构建失败")
        except BaseException as exc:
            failure = exc
            raise
        finally:
            _cleanup_preserving_failure(cleanup_build_cache, failure)
    _check_result(run(profile, "up", "-d", "--no-build"), "Docker 启动失败")
    cleanup_images()


def down(previously_running=None) -> None:
    """删除本次启动的容器和日志，保留启动前运行的服务、镜像和挂载数据。"""
    if threading.current_thread() is not threading.main_thread():
        _down(previously_running)
        return
    handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        _down(previously_running)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def _down(previously_running) -> None:
    if not previously_running:
        _check_result(run("full", "down", "--remove-orphans", timeout=120), "Docker 停止失败")
    else:
        services = sorted(set(REQUIRED_SERVICES["full"]) - set(previously_running))
        if services:
            _check_result(run("full", "stop", "--timeout", "20", *services), "Docker 停止失败")
            _check_result(run("full", "rm", "--force", *services), "Docker 容器清理失败")
    cleanup_containers()
    cleanup_images()
    cleanup_build_cache(prune=False)


def _images_ready(profile: str) -> bool:
    return not _stale_services()


def _ps(profile: str = "chatbot") -> Dict[str, Dict[str, str]]:
    result = run(profile, "ps", "--format", "json", timeout=30)
    _check_result(result, "读取 Docker 容器状态失败")
    rows = {}
    try:
        decoded = json.loads(result.stdout)
    except ValueError:
        decoded = None
    if isinstance(decoded, list):
        entries = decoded
    else:
        entries = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    for row in entries:
        service = row.get("Service") or ""
        if service:
            rows[service] = row
    return rows


def running_services(profile: str = "chatbot") -> Set[str]:
    return {name for name, row in _ps(profile).items() if (row.get("State") or "").lower() == "running"}


def wait_ready(profile: str, timeout: float = 420) -> None:
    """Wait for every required service to be running and healthchecked."""
    import time

    required = REQUIRED_SERVICES[profile]
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = _ps(profile)
        ready = True
        for service in required:
            row = last.get(service) or {}
            running = (row.get("State") or "").lower() == "running"
            health = (row.get("Health") or "").lower()
            if not running or (service in HEALTH_REQUIRED and health != "healthy"):
                ready = False
                break
        if ready:
            return
        time.sleep(2)
    states = ", ".join(
        f"{name}={row.get('State', '?')}/{row.get('Health') or '-'}"
        for name, row in sorted(last.items())
    )
    raise DockerError(f"Docker 服务未在 {int(timeout)} 秒内就绪：{states or '没有容器'}")


def follow(profile: str) -> None:
    """Stream logs until Ctrl+C; containers stay managed by the caller."""
    command = compose_command(profile, "logs", "-f", "--tail", "80")
    process = subprocess.Popen(command, cwd=ROOT, env=_environment(profile), **_process_options())
    try:
        process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def service_logs(profile: str, service: str, tail: int = 80) -> str:
    result = run(profile, "logs", "--tail", str(tail), "--no-color", service, timeout=60)
    return (result.stdout or "") + (result.stderr or "")
