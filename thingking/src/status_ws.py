import threading
import time
import atexit
from urllib.parse import quote
from uuid import uuid4

from ark_mcp_client import ArkMcpError, call_ark, mcp_config

_status_ws_thread = None
_status_lock = threading.Lock()
_status_stop = threading.Event()
_instance_id = str(uuid4())


def _bines_status_ws_url(base_url: str, secret_token: str) -> str | None:
    """根据 MOMENTS_API_BASE_URL 生成状态 WebSocket 地址。"""
    base = (base_url or "").rstrip("/")
    if not base:
        return None

    if base.startswith("https://"):
        ws_base = "wss://" + base[8:]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[7:]
    else:
        ws_base = "wss://" + base

    secret = (secret_token or "").strip()
    if secret:
        return f"{ws_base}/api/status/bines/ws?secret={quote(secret, safe='')}"
    return f"{ws_base}/api/status/bines/ws"


def _status_ws_worker(base_url: str, secret_token: str):
    """
    通过 WebSocket 连接 /api/status/bines/ws 维持在线状态：
    - 连接成功时后端将 online 设为 true 并可能下发 status 消息
    - 连接关闭/错误时后端自动将 online 设为 false，本端只负责重连
    """
    try:
        import websocket
    except ImportError:
        print("[Thinking] 未安装 websocket-client，无法建立状态 WebSocket，请 pip install websocket-client", flush=True)
        return

    while True:
        url = _bines_status_ws_url(base_url, secret_token)
        if not url:
            print("[Thinking] MOMENTS_API_BASE_URL 未配置，跳过状态 WebSocket", flush=True)
            return

        try:
            print(f"[Thinking] 尝试建立状态 WebSocket: {url.split('?')[0]}", flush=True)
            ws = websocket.WebSocketApp(
                url,
                header={"X-Status-Secret": secret_token or ""},
                on_open=lambda w: print("[Thinking] 状态 WebSocket 已连接（online=true 由后端维护）", flush=True),
                on_message=lambda w, m: None,
                on_error=lambda w, e: print(f"[Thinking] 状态 WebSocket 错误: {e}", flush=True),
                on_close=lambda w, code, msg: print(f"[Thinking] 状态 WebSocket 已断开 (code={code})", flush=True),
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[Thinking] 状态 WebSocket 异常，将在稍后重试: {e}", flush=True)

        time.sleep(5)


def _status_mcp_worker():
    had_error = False
    while not _status_stop.is_set():
        try:
            call_ark("ark_heartbeat", {"instance_id": _instance_id, "online": True})
            if had_error:
                print("[Thinking] Ark MCP 在线状态已恢复", flush=True)
            had_error = False
        except ArkMcpError as error:
            if not had_error:
                print(f"[Thinking] Ark MCP 状态续租失败 ({error.code})，不会回落旧 WebSocket", flush=True)
            had_error = True
        _status_stop.wait(30)
    # Release this instance only. If unreachable, the 90s lease expires.
    try:
        call_ark("ark_heartbeat", {"instance_id": _instance_id, "online": False})
    except ArkMcpError:
        pass


def stop_status_stream():
    _status_stop.set()
    if _status_ws_thread and _status_ws_thread.is_alive():
        _status_ws_thread.join(timeout=1)


atexit.register(stop_status_stream)


def ensure_status_stream_started(base_url: str, secret_token: str):
    """沿用运行时入口：已配置 MCP 则续租，否则显式启用旧 WebSocket。"""
    global _status_ws_thread
    with _status_lock:
        if _status_ws_thread is not None and _status_ws_thread.is_alive():
            return
        try:
            use_mcp = mcp_config() is not None
        except ArkMcpError as error:
            print(f"[Thinking] {error}", flush=True)
            return
        _status_stop.clear()
        print(f"[Thinking] Ark 状态模式：{'MCP 租约' if use_mcp else 'legacy WebSocket'}", flush=True)
        _status_ws_thread = threading.Thread(
            target=_status_mcp_worker if use_mcp else _status_ws_worker,
            args=() if use_mcp else (base_url, secret_token),
            name="ark-presence", daemon=True,
        )
        _status_ws_thread.start()
