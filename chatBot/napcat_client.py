"""Single reconnect owner; API responses never wait for event handlers."""
import json
import logging
import queue
import threading
import uuid
from chatBot.config import ONEBOT_WS_URL, ONEBOT_ACCESS_TOKEN

log = logging.getLogger(__name__)


class NapCatClient:
    def __init__(self, ws_url=ONEBOT_WS_URL, token=ONEBOT_ACCESS_TOKEN, factory=None):
        self.ws_url, self.token = ws_url, token
        self._factory = factory
        self.ws = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending = {}
        self._listeners = []
        self._events = queue.Queue()
        self._thread = None
        self._event_thread = None

    @property
    def is_connected(self):
        return self._connected.is_set()

    def connect(self, background=True):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._events = queue.Queue()
            self._event_thread = threading.Thread(target=self._dispatch, daemon=True, name="napcat-events")
            self._event_thread.start()
            self._thread = threading.Thread(target=self._run, daemon=True, name="napcat-connection")
            self._thread.start()
        if not background:
            self._thread.join()

    def _run(self):
        delay = 1
        while not self._stop.is_set():
            self._opened = False
            if self._factory is None:
                import websocket
                factory = websocket.WebSocketApp
            else:
                factory = self._factory
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            self.ws = factory(
                self.ws_url, header=headers, on_open=self._on_open,
                on_message=self._on_message, on_error=self._on_error, on_close=self._on_close,
            )
            try:
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                log.warning("NapCat connection unavailable")
            self._on_close(self.ws, None, None)
            if self._opened:
                delay = 1
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30)

    def disconnect(self):
        self._stop.set()
        if self.ws:
            self.ws.close()
        self._on_close(self.ws, None, None)
        self._events.put(None)
        for thread in (self._thread, self._event_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)

    def _on_open(self, ws):
        self._opened = True
        self._connected.set()
        log.info("NapCat connected")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if not isinstance(data, dict):
                return
        except (TypeError, ValueError):
            log.warning("Invalid NapCat frame")
            return
        if "echo" in data and "post_type" not in data:
            with self._lock:
                pending = self._pending.get(str(data["echo"]))
                if pending:
                    # NapCat streaming actions send stream chunks then a final response.
                    stream_type = (data.get("data") or {}).get("type") if isinstance(data.get("data"), dict) else None
                    if data.get("stream") == "stream-action" and stream_type in {"stream", "reset"}:
                        pending["chunks"].append(data)
                    else:
                        pending["result"] = data
                        if pending["chunks"]:
                            data["stream_chunks"] = pending["chunks"]
                        pending["event"].set()
            return
        if data.get("post_type") != "meta_event":
            with self._lock:
                if self._events.qsize() >= 10000:
                    # Reconnect rather than unbounded RAM growth if persistence is stalled.
                    self.ws.close()
                    return
            self._events.put(data)

    def _dispatch(self):
        while not self._stop.is_set():
            event = self._events.get()
            if event is None:
                return
            for listener in tuple(self._listeners):
                try:
                    listener(event)
                except Exception:
                    log.exception("NapCat event persistence failed")

    def _on_error(self, ws, error):
        log.warning("NapCat WebSocket error")

    def _on_close(self, ws, code, message):
        self._connected.clear()
        with self._lock:
            for pending in self._pending.values():
                if not pending["event"].is_set():
                    pending["result"] = {"status": "uncertain", "retcode": -1, "msg": "连接中断；请求可能已执行"}
                    pending["event"].set()

    def add_event_listener(self, callback):
        self._listeners.append(callback)

    def on(self, event_type=None):
        def decorate(function):
            self.add_event_listener(lambda event: function(event) if event_type is None or event.get("post_type") == event_type else None)
            return function
        return decorate

    def raw_api(self, action, params=None, timeout=30):
        if not self._connected.wait(min(timeout, 5)):
            return {"status": "failed", "retcode": -1, "msg": "NapCat 未连接", "dispatched": False}
        echo = str(uuid.uuid4())
        pending = {"event": threading.Event(), "result": None, "chunks": []}
        with self._lock:
            self._pending[echo] = pending
        try:
            with self._send_lock:
                self.ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            if not pending["event"].wait(timeout):
                return {"status": "uncertain", "retcode": -1, "msg": "API 超时；请求可能已执行", "echo": echo}
            return pending["result"]
        except Exception:
            return {"status": "uncertain", "retcode": -1, "msg": "发送中断；不能自动重发", "echo": echo}
        finally:
            with self._lock:
                self._pending.pop(echo, None)

    _call_api = raw_api

    def send_private_msg(self, user_id, message):
        return self.raw_api("send_private_msg", {"user_id": user_id, "message": message})

    def send_group_msg(self, group_id, message, at_user_id=None):
        chain = [{"type": "text", "data": {"text": message}}] if isinstance(message, str) else list(message)
        if at_user_id:
            chain.insert(0, {"type": "at", "data": {"qq": str(at_user_id)}})
        return self.raw_api("send_group_msg", {"group_id": group_id, "message": chain})

    def __getattr__(self, action):
        if action.startswith("_"):
            raise AttributeError(action)
        # New NapCat APIs do not require Python wrapper releases.
        return lambda **params: self.raw_api(action, params)
