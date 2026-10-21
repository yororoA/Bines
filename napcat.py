"""One-command isolated NapCat lifecycle; credentials never printed."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from common.env import load_env, read_env
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "deploy" / "napcat"


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
    if args.action in {"init", "up"}:
        initialize()
    if args.action == "init":
        return
    if args.action == "webui":
        import webbrowser
        webbrowser.open("http://127.0.0.1:6099")
        print("WebUI token 存于 deploy/napcat/.env 的 NAPCAT_WEBUI_TOKEN；首次需扫码。")
        return
    if not shutil.which("docker"):
        raise SystemExit("未找到 Docker。请安装并启动 Docker Desktop 后再执行 python napcat.py up。")
    command = ["docker", "compose", "--project-directory", str(DEPLOY), "-f", str(DEPLOY / "compose.yaml")]
    operations = {"up": ["up", "-d"], "down": ["down"], "logs": ["logs", "--tail", "100", "-f"],
                  "status": ["ps"], "config": ["config", "--quiet"], "update": ["pull"]}
    subprocess.run(command+operations[args.action], check=True)
    if args.action == "update":
        subprocess.run(command+["up", "-d"], check=True)
    if args.action == "up":
        print("NapCat 已启动；访问 http://127.0.0.1:6099 扫码登录。")


if __name__ == "__main__":
    main()
