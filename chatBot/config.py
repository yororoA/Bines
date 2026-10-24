"""One configuration for the gateway, Thinking and Docker launcher."""
import os
from pathlib import Path
import config as defaults

ROOT = defaults.ROOT_DIR
DATA_DIR = Path(os.environ.get("BINES_MEMORY_DIR", str(ROOT / "thingking" / "memory_data")))
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT / DATA_DIR
GATEWAY_DIR = Path(os.environ.get("BINES_GATEWAY_DIR", str(DATA_DIR)))
if not GATEWAY_DIR.is_absolute():
    GATEWAY_DIR = ROOT / GATEWAY_DIR
ONEBOT_WS_URL = defaults.NAPCAT_WS_URL
ONEBOT_ACCESS_TOKEN = defaults.NAPCAT_ACCESS_TOKEN
BOT_QQ_ID = os.environ.get("BINES_BOT_QQ_ID", "")
OWNER_QQ_IDS = frozenset(filter(None, (
    item.strip() for item in os.environ.get("BINES_OWNER_QQ_IDS", "").split(",")
)))
# Legacy import compatibility. Group administrators never imply Bines ownership.
ADMIN_QQ_LIST = list(OWNER_QQ_IDS)
GATEWAY_HOST = os.environ.get("BINES_QQ_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.environ.get("BINES_QQ_GATEWAY_PORT", "6102"))
GATEWAY_URL = os.environ.get("BINES_QQ_GATEWAY_URL", f"http://127.0.0.1:{GATEWAY_PORT}")
WEBUI_URL = os.environ.get("BINES_NAPCAT_WEBUI_URL", "http://127.0.0.1:6099")
WEBUI_TOKEN = os.environ.get("BINES_NAPCAT_WEBUI_TOKEN", "")
DEBOUNCE = float(os.environ.get("BINES_QQ_DEBOUNCE", "1.5"))
MAX_WAIT = float(os.environ.get("BINES_QQ_MAX_WAIT", "5"))
WORKERS = int(os.environ.get("BINES_QQ_WORKERS", "4"))
GROUP_REPLY_MODE = os.environ.get("BINES_QQ_GROUP_REPLY", "mention")
# Conversation needs more variation than low-temperature extraction/tool tasks.
REPLY_TEMPERATURE = float(os.environ.get("BINES_QQ_TEMPERATURE", "0.85"))
if not 0 <= REPLY_TEMPERATURE <= 2:
    raise ValueError("BINES_QQ_TEMPERATURE 必须在 0 到 2 之间")


def gateway_token():
    token = os.environ.get("BINES_QQ_GATEWAY_TOKEN", "")
    if token:
        return token
    path = DATA_DIR / "gateway.token"
    if path.exists():
        import time
        for _ in range(100):
            value = path.read_text().strip()
            if value:
                return value
            time.sleep(0.01)  # A concurrently starting gateway may be creating the token.
        raise RuntimeError("gateway.token 为空；请修复本机网关令牌")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    import secrets
    try:
        with path.open("x") as stream:
            path.chmod(0o600)
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        return gateway_token()
    return path.read_text().strip()
