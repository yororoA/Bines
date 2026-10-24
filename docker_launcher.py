"""Docker Compose orchestration used by ``python bines.py``.

The QQ chain runs in containers; full mode still starts host-only desktop
services after the containers are healthy.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
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
HEALTH_REQUIRED = {"chatbot", "memory-server", "rag-web", "napcat"}


class DockerError(RuntimeError):
    pass


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


# 每个镜像分别计算内容哈希，QQ 网关独有代码变化不再触发 RAG 镜像重建。
_SHARED_SOURCES = (
    "*.py", "common/**/*.py", "tools/**/*.py",
    "thingking/*.py", "thingking/src/**/*.py", "thingking/scripts/*.py", "thingking/persona/*.yaml",
    "skills/**/*.md", "server/skill_actions.json", ".dockerignore",
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
    "docker_launcher.py",
    "website_bridge.py",
    "website_runtime.py",
})
IMAGE_HASH_EXCLUDES_BY_IMAGE = {
    "bines-thinking:local": frozenset({
        "common/website_spool.py",
        "thingking/qq_thinking.py",
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
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        raise DockerError(label + "：\n" + "\n".join(detail))


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
    return compose_command(profile, "build", "--builder", BUILDER_NAME, *services)


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


def cleanup_build_cache() -> None:
    driver = _builder_driver()
    if driver is None:
        return
    if driver != "docker-container":
        raise DockerError("跳过共享构建器 bines 的缓存清理")
    # 删除专用构建器及其状态卷，已停止的构建器也能释放；不保留空缓存卷。
    _check_result(
        _docker("buildx", "rm", "--force", BUILDER_NAME, timeout=180),
        "释放 Bines 构建缓存失败",
    )


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
        try:
            _check_result(run(profile, "build", "--builder", BUILDER_NAME, *stale), "Docker 构建失败")
        finally:
            cleanup_build_cache()
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
    cleanup_build_cache()


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
            if not running or (service in HEALTH_REQUIRED and health not in {"", "healthy"}):
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
