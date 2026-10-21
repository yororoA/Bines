"""One-command isolated NapCat lifecycle; credentials never printed."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from common.env import load_env, read_env
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "deploy" / "napcat"
DEFAULT_IMAGE = "mlikiowa/napcat-docker:latest"


def docker_executable():
    """Find Docker Desktop's CLI even before a newly opened shell refreshes PATH."""
    found = shutil.which("docker")
    if found:
        return found
    candidates = []
    if sys.platform == "darwin":
        candidates = [
            Path.home() / ".docker" / "bin" / "docker",
            Path("/Applications/Docker.app/Contents/Resources/bin/docker"),
            Path.home() / "Applications" / "Docker.app" / "Contents" / "Resources" / "bin" / "docker",
        ]
    elif os.name == "nt":
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Docker" / "Docker" / "resources" / "bin" / "docker.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Docker" / "resources" / "bin" / "docker.exe",
        ]
    return str(next((path for path in candidates if path.is_file()), "")) or None


def docker_desktop_app():
    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/Docker.app"),
            Path.home() / "Applications" / "Docker.app",
        ]
    elif os.name == "nt":
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Docker" / "Docker" / "Docker Desktop.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Docker" / "Docker Desktop.exe",
        ]
    else:
        candidates = []
    return next((path for path in candidates if path.exists()), None)


def start_docker_desktop():
    app = docker_desktop_app()
    if not app:
        return False
    if sys.platform == "darwin":
        subprocess.run(["open", str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(
            [str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    return True


def ensure_docker_ready(timeout=180, cancel=None):
    docker = docker_executable()
    if not docker:
        raise RuntimeError("未找到 Docker；请重新运行依赖安装器，或配置 BINES_NAPCAT_DEPLOY=external")

    def ready():
        try:
            result = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    if ready():
        return docker
    if not start_docker_desktop():
        raise RuntimeError("Docker 引擎未运行，且无法找到 Docker Desktop")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("等待 Docker 启动已取消")
        if ready():
            return docker
        time.sleep(2)
    raise RuntimeError("Docker Desktop 尚未就绪；请在其窗口接受协议并完成首次初始化")


def image_name():
    values = read_env(DEPLOY / ".env") if (DEPLOY / ".env").exists() else {}
    return values.get("NAPCAT_IMAGE") or os.environ.get("NAPCAT_IMAGE") or DEFAULT_IMAGE


def image_available(docker=None):
    docker = docker or docker_executable()
    if not docker:
        return False
    try:
        result = subprocess.run(
            [docker, "image", "inspect", image_name()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def write_secret(path, value):
    # The supervisor may load .env in several child startups simultaneously.
    # Atomic replacement prevents readers from seeing a partially written file.
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(settings_path=None):
    load_env()
    data = DEPLOY / "data"
    for folder in ("qq", "config", "plugins", "media", "media/public"):
        (data / folder).mkdir(parents=True, exist_ok=True)
    values = {}
    env_path = DEPLOY / ".env"
    if env_path.exists():
        values = read_env(env_path)
    settings_path = Path(settings_path) if settings_path else ROOT / ".env"
    root_values = read_env(settings_path)
    values.setdefault("NAPCAT_ACCOUNT", root_values.get("BINES_BOT_QQ_ID", os.environ.get("BINES_BOT_QQ_ID", "")))
    values.setdefault("NAPCAT_WS_TOKEN", root_values.get("BINES_NAPCAT_ACCESS_TOKEN") or secrets.token_urlsafe(32))
    values.setdefault("NAPCAT_WEBUI_TOKEN", root_values.get("BINES_NAPCAT_WEBUI_TOKEN") or secrets.token_urlsafe(32))
    values.setdefault("NAPCAT_WS_PORT", str(urlsplit(root_values.get("BINES_NAPCAT_WS_URL", "")).port or 6101))
    values.setdefault("NAPCAT_WEBUI_PORT", str(urlsplit(root_values.get("BINES_NAPCAT_WEBUI_URL", "")).port or 6099))
    values.setdefault("NAPCAT_UID", str(os.getuid() if hasattr(os, "getuid") else 1000))
    values.setdefault("NAPCAT_GID", str(os.getgid() if hasattr(os, "getgid") else 1000))
    write_secret(env_path, "\n".join(f"{key}={value}" for key, value in values.items())+"\n")
    onebot = {
        "network": {"httpServers": [], "httpSseServers": [], "httpClients": [],
                    "websocketClients": [], "websocketServers": [{
                        "name": "Bines", "enable": True, "host": "0.0.0.0", "port": 3001,
                        "token": values["NAPCAT_WS_TOKEN"], "reportSelfMessage": True,
                        "enableForcePushEvent": True, "heartInterval": 30000,
                        "messagePostFormat": "array",
                    }]},
        "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False,
    }
    # Existing live configuration is preserved. Account overrides inherit the generated template.
    targets = ["onebot11.json"]
    if values["NAPCAT_ACCOUNT"]:
        targets.append(f"onebot11_{values['NAPCAT_ACCOUNT']}.json")
    for name in targets:
        path = data / "config" / name
        if not path.exists():
            write_secret(path, json.dumps(onebot, indent=2))
    webui = data / "config" / "webui.json"
    if not webui.exists():
        write_secret(webui, json.dumps({"host": "0.0.0.0", "port": 6099, "token": values["NAPCAT_WEBUI_TOKEN"]}, indent=2))
    env = settings_path
    text = env.read_text() if env.exists() else ""
    additions = {
        "BINES_NAPCAT_WS_URL": f"ws://127.0.0.1:{values['NAPCAT_WS_PORT']}",
        "BINES_NAPCAT_ACCESS_TOKEN": values["NAPCAT_WS_TOKEN"],
        "BINES_NAPCAT_WEBUI_URL": f"http://127.0.0.1:{values['NAPCAT_WEBUI_PORT']}",
        "BINES_NAPCAT_WEBUI_TOKEN": values["NAPCAT_WEBUI_TOKEN"],
    }
    # Fill empty NapCat assignments too (for installations created from a template).
    import re
    for key, value in additions.items():
        if key in root_values and not root_values[key]:
            text = re.sub(rf"(?m)^\s*(?:export\s+)?{re.escape(key)}\s*=.*$", f"{key}={value}", text)
    defined = set(root_values)
    text += ("\n" if text and not text.endswith("\n") else "") + "".join(
        f"{key}={value}\n" for key, value in additions.items() if key not in defined)
    write_secret(env, text)
    print("NapCat 配置已就绪；登录数据、配置、插件和媒体均独立持久化。")


def main():
    load_env()
    parser = argparse.ArgumentParser(description="NapCat Docker 部署管理")
    parser.add_argument("action", choices=["init", "up", "down", "logs", "status", "update", "config", "webui"])
    args = parser.parse_args()
    if args.action in {"init", "up", "webui"}:
        initialize()
    if args.action == "init":
        return
    if args.action == "webui":
        import webbrowser
        values = read_env(DEPLOY / ".env")
        port = values.get("NAPCAT_WEBUI_PORT", "6099")
        token = quote(values.get("NAPCAT_WEBUI_TOKEN", ""), safe="")
        webbrowser.open(f"http://127.0.0.1:{port}/webui?token={token}")
        print("已使用 deploy/napcat/.env 中的 token 打开 NapCat WebUI。")
        return
    docker = docker_executable()
    if not docker:
        raise SystemExit("未找到 Docker。请安装并启动 Docker Desktop 后再执行 python napcat.py up。")
    if args.action in {"up", "update"}:
        docker = ensure_docker_ready()
    command = [docker, "compose", "--project-directory", str(DEPLOY), "-f", str(DEPLOY / "compose.yaml")]
    if args.action == "up" and not image_available(docker):
        print("首次运行正在下载 NapCat 镜像，可能需要数分钟。", flush=True)
        subprocess.run(command+["pull", "napcat"], check=True)
    operations = {"up": ["up", "-d", "--pull", "never"], "down": ["down"], "logs": ["logs", "--tail", "100", "-f"],
                  "status": ["ps"], "config": ["config", "--quiet"], "update": ["pull"]}
    subprocess.run(command+operations[args.action], check=True)
    if args.action == "update":
        subprocess.run(command+["up", "-d"], check=True)
    if args.action == "up":
        print("NapCat 已启动；另开终端运行 python3 napcat.py logs，扫描日志中的二维码。")
        print("管理页使用 python3 napcat.py webui 打开；6099 WebUI 需要 token，不显示登录二维码。")


if __name__ == "__main__":
    main()
