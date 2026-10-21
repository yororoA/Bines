"""Docker Compose orchestration used by ``python bines.py``.

The QQ chain runs in containers; full mode still starts host-only desktop
services after the containers are healthy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Dict, List, Set

ROOT = Path(__file__).resolve().parent
COMPOSE_FILE = ROOT / "deploy" / "docker" / "compose.yaml"
NAPCAT_ENV = ROOT / "deploy" / "napcat" / ".env"

REQUIRED_SERVICES = {
    "chatbot": ("napcat", "chatbot", "qq-thinking", "memory-worker"),
    "full": ("napcat", "chatbot", "qq-thinking", "memory-worker", "memory-server", "rag-web"),
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
    env = dict(os.environ)
    env["BINES_PROFILE"] = profile
    env.setdefault("BINES_UID", str(os.getuid() if hasattr(os, "getuid") else 1000))
    env.setdefault("BINES_GID", str(os.getgid() if hasattr(os, "getgid") else 1000))
    return env


def run(profile: str, *args: str, timeout: float = 3600) -> subprocess.CompletedProcess:
    command = compose_command(profile, *args)
    return subprocess.run(
        command, cwd=ROOT, env=_environment(profile), text=True,
        capture_output=True, timeout=timeout,
    )


def _images_ready(profile: str) -> bool:
    docker = docker_executable()
    if not docker:
        return False
    images = ["bines-launcher:local"]
    if profile == "full":
        images.append("bines-thinking:local")
    for image in images:
        result = subprocess.run(
            [docker, "image", "inspect", image],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode:
            return False
    return True


def up(profile: str) -> None:
    """Start the chain in the background; build only when an image is missing."""
    args = ["up", "-d"] + ([] if _images_ready(profile) else ["--build"])
    result = run(profile, *args)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        raise DockerError("Docker 启动失败：\n" + "\n".join(detail))


def down() -> None:
    # Enable every profile so full-mode containers are removed too.
    result = run("full", "down", "--remove-orphans", timeout=120)
    if result.returncode:
        raise DockerError((result.stderr or result.stdout or "Docker 停止失败").strip())


def _ps(profile: str = "chatbot") -> Dict[str, Dict[str, str]]:
    result = run(profile, "ps", "--format", "json", timeout=30)
    if result.returncode:
        return {}
    rows = {}
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        service = row.get("Service") or ""
        if service:
            rows[service] = row
    return rows


def running_services(profile: str = "chatbot") -> Set[str]:
    return {name for name, row in _ps(profile).items() if (row.get("State") or "").lower() == "running"}


def wait_ready(profile: str, timeout: float = 240) -> None:
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
    subprocess.run(command, cwd=ROOT, env=_environment(profile), check=False)


def service_logs(profile: str, service: str, tail: int = 80) -> str:
    result = run(profile, "logs", "--tail", str(tail), "--no-color", service, timeout=60)
    return (result.stdout or "") + (result.stderr or "")
