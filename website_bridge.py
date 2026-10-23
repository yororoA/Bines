"""Authenticated host bridge between containerized Thinking and local Chrome."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import signal
import threading
import time

from common.website import AgentBrowser, PlanStore, WebsiteService
from common.website_spool import (
    acquire_bridge_lock,
    atomic_write_json,
    heartbeat,
    read_json,
    signed_envelope,
    verify_envelope,
)


ROOT = Path(__file__).resolve().parent
MAX_REQUEST_BYTES = 1024 * 1024


class UnknownRoute(ValueError):
    pass


def dispatch(service, route, body):
    lock = getattr(service, "execute_lock", None)
    with lock if lock is not None else nullcontext():
        return _dispatch(service, route, body)


def _dispatch(service, route, body):
    if route == "/v1/status":
        return service.status(body.get("platform", ""))
    if route == "/v1/open":
        return service.open(
            body.get("platform"), body.get("url", ""), bool(body.get("snapshot", True)),
        )
    if route == "/v1/snapshot":
        return service.snapshot(
            body.get("platform"), bool(body.get("interactive", True)),
        )
    if route == "/v1/scroll":
        return service.scroll(
            body.get("platform"), body.get("direction", "down"), body.get("pixels", 600),
        )
    if route == "/v1/interact":
        return service.interact(
            body.get("platform"), body.get("action"), body.get("ref", ""),
            body.get("value", ""), body.get("operation_id", ""), body.get("contact_allowed", True),
        )
    if route == "/v1/media-status":
        return service.media_status(body.get("platform"))
    if route == "/v1/tabs":
        return service.tabs(body.get("platform"))
    if route == "/v1/plans/create":
        return service.create_plan(
            body.get("platform"), body.get("action"), body.get("content"),
            title=body.get("title", ""),
            target_url=body.get("target_url", ""),
            media_paths=body.get("media_paths"),
            steps=body.get("steps"),
        )
    if route == "/v1/plans/get":
        return service.get_plan(body.get("plan_id"))
    if route == "/v1/plans/approve":
        return service.approve(body.get("plan_id"))
    if route == "/v1/plans/execute":
        if body.get("autonomous") is True:
            plan = service.get_plan(body.get("plan_id"))
            if plan.get("plan_status") == "pending_approval":
                service.approve(body.get("plan_id"))
        return service.execute(body.get("plan_id"))
    raise UnknownRoute("unknown route")


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "BinesWebsiteBridge/1"

    def log_message(self, _format, *_args):
        return

    def _authorized(self):
        expected = "Bearer " + self.server.token
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def _json(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if not 0 <= length <= MAX_REQUEST_BYTES:
            raise ValueError("请求体过大")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def do_GET(self):
        if not self._authorized():
            self._json(401, {"status": "error", "error": "unauthorized"})
            return
        if self.path != "/health":
            self._json(404, {"status": "error", "error": "not_found"})
            return
        self._json(200, {"status": "ok", "service": "website-bridge"})

    def do_POST(self):
        if not self._authorized():
            self._json(401, {"status": "error", "error": "unauthorized"})
            return
        try:
            body = self._body()
            route = self.path.rstrip("/")
            result = dispatch(self.server.service, route, body)
            self._json(200, result)
        except UnknownRoute:
            self._json(404, {"status": "error", "error": "not_found"})
        except (TypeError, ValueError) as exc:
            self._json(400, {"status": "error", "error": str(exc)})
        except Exception as exc:
            self._json(500, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, token, service):
        super().__init__(address, handler)
        self.token = token
        self.service = service


class SpoolWorker:
    def __init__(self, service, root, token):
        self.service = service
        self.root = Path(root)
        self.token = token
        self.requests = self.root / "requests"
        self.processing = self.root / "processing"
        self.responses = self.root / "responses"
        self.stop = threading.Event()
        self.thread = None
        self.heartbeat_thread = None
        self.owner_lock = None

    def acquire(self):
        if self.owner_lock is None:
            self.owner_lock = acquire_bridge_lock(self.root / ".worker.lock")

    def start(self):
        self.acquire()
        for directory in (self.root, self.requests, self.processing, self.responses):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        for path in self.processing.glob("*.json"):
            if time.time() - path.stat().st_mtime > 60:
                destination = self.requests / path.name
                if destination.exists():
                    path.unlink(missing_ok=True)
                else:
                    os.replace(path, destination)
        atomic_write_json(self.root / "heartbeat.json", heartbeat(self.token))
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat, name="WebsiteHeartbeat", daemon=True,
        )
        self.heartbeat_thread.start()
        self.thread = threading.Thread(target=self._run, name="WebsiteSpool", daemon=True)
        self.thread.start()

    def _respond(self, request_id, status_code, result):
        fields = {
            "id": request_id,
            "status_code": int(status_code),
            "result": result,
            "created_at": time.time(),
        }
        atomic_write_json(
            self.responses / f"{request_id}.json",
            signed_envelope(self.token, "response", fields),
        )

    def _process(self, path):
        request_id = path.stem
        if len(request_id) != 32 or any(char not in "0123456789abcdef" for char in request_id):
            path.unlink(missing_ok=True)
            return
        claimed = self.processing / path.name
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            return
        try:
            envelope = read_json(claimed)
            if not verify_envelope(self.token, "request", envelope):
                self._respond(request_id, 401, {"status": "error", "error": "unauthorized"})
                return
            if envelope.get("id") != request_id:
                self._respond(request_id, 400, {"status": "error", "error": "request id mismatch"})
                return
            created_at = float(envelope.get("created_at") or 0)
            if abs(time.time() - created_at) > 15 * 60:
                self._respond(request_id, 408, {"status": "error", "error": "request expired"})
                return
            route = str(envelope.get("route") or "").rstrip("/")
            body = envelope.get("payload")
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            try:
                result = dispatch(self.service, route, body)
                self._respond(request_id, 200, result)
            except UnknownRoute:
                self._respond(request_id, 404, {"status": "error", "error": "not_found"})
            except (TypeError, ValueError) as exc:
                self._respond(request_id, 400, {"status": "error", "error": str(exc)})
            except Exception as exc:
                self._respond(
                    request_id, 500,
                    {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                )
        except Exception as exc:
            self._respond(
                request_id, 500,
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
            )
        finally:
            claimed.unlink(missing_ok=True)

    def _heartbeat(self):
        # Browser navigation/uploads can block the request worker for minutes.
        while not self.stop.wait(1):
            try:
                atomic_write_json(self.root / "heartbeat.json", heartbeat(self.token))
            except OSError:
                pass

    def _run(self):
        while not self.stop.is_set():
            now = time.time()
            for path in sorted(self.requests.glob("*.json"))[:10]:
                if self.stop.is_set():
                    break
                try:
                    self._process(path)
                except OSError:
                    pass
            for path in self.responses.glob("*.json"):
                try:
                    if now - path.stat().st_mtime > 3600:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.stop.wait(0.05)

    def close(self):
        self.stop.set()
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)
            self.heartbeat_thread = None
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None
        if self.owner_lock:
            (self.root / "heartbeat.json").unlink(missing_ok=True)
            self.owner_lock.close()
            self.owner_lock = None


def create_service():
    data_dir = Path(os.environ.get(
        "BINES_WEBSITE_DATA_DIR",
        str(ROOT / "static" / "Bines Website Automation"),
    )).expanduser()
    command = shlex.split(os.environ.get("BINES_AGENT_BROWSER_COMMAND", "agent-browser"))
    if not command:
        raise ValueError("BINES_AGENT_BROWSER_COMMAND 不能为空")
    executable = os.environ.get("BINES_CHROME_FOR_TESTING_EXECUTABLE", "")
    if not executable or not Path(executable).is_file():
        raise ValueError("必须由统一启动器提供 Chrome for Testing 可执行文件")
    headed = os.environ.get("BINES_WEBSITE_HEADED", "true").lower() not in {"0", "false", "no"}
    browser = AgentBrowser(
        command,
        profile_root=data_dir / "profiles",
        executable_path=executable,
        headed=headed,
    )
    return WebsiteService(
        PlanStore(data_dir / "website_outbox.sqlite3"),
        browser,
        root=ROOT,
    )


def main():
    parser = argparse.ArgumentParser(description="Bines website browser host bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--spool", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or len(args.token) < 32:
        raise SystemExit("port 或 token 无效")
    spool = SpoolWorker(None, args.spool, args.token) if args.spool else None
    if spool:
        spool.acquire()
    try:
        service = create_service()
        server = BridgeServer((args.host, args.port), BridgeHandler, token=args.token, service=service)
    except BaseException:
        if spool:
            spool.close()
        raise
    if spool:
        spool.service = service
        spool.start()

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.parent_pid:
        def watch_parent():
            while True:
                try:
                    os.kill(args.parent_pid, 0)
                except OSError:
                    server.shutdown()
                    return
                threading.Event().wait(1)
        threading.Thread(target=watch_parent, name="WebsiteParentWatch", daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        if spool:
            spool.close()
        server.server_close()
        service.browser.close()


if __name__ == "__main__":
    main()
