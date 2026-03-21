import threading
import time
from urllib.parse import quote

_status_ws_thread = None


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
            time.sleep(10)
            continue

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


def ensure_status_stream_started(base_url: str, secret_token: str):
    """启动 Bines 在线状态 WebSocket 连接（若尚未启动）。"""
    global _status_ws_thread
    if _status_ws_thread is not None and _status_ws_thread.is_alive():
        return
    _status_ws_thread = threading.Thread(
        target=_status_ws_worker,
        args=(base_url, secret_token),
        daemon=True,
    )
    _status_ws_thread.start()
