"""Authenticated loopback capability gateway. Only this process owns OneBot."""
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from chatBot.config import GATEWAY_HOST, GATEWAY_PORT, gateway_token
from chatBot.messages import LOCAL_IDENTITY


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, service, address=None, token=None, memory_hub=None):
        self.service = service
        self.memory_hub = memory_hub
        self.token = token or gateway_token()
        super().__init__(address or (GATEWAY_HOST, GATEWAY_PORT), GatewayHandler)


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {self.server.token}"):
            return self.respond(401, {"error": "authentication required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4*1024*1024:
                raise ValueError("请求大小无效")
            data = json.loads(self.rfile.read(length))
            identity = LOCAL_IDENTITY
            event_id = data.get("event_id")
            if event_id:
                with self.server.service.store.connection() as db:
                    row = db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()
                if not row:
                    raise PermissionError("未知 QQ 事件")
                identity = self.server.service.store.identity(json.loads(row["raw"]))
            service = self.server.service
            if self.path == "/health":
                result = {"connected": service.client.is_connected}
            elif self.path == "/memory":
                if self.server.memory_hub is None:
                    raise RuntimeError("Memory hub unavailable")
                result = self.server.memory_hub.call(identity, data["method"], data.get("params", {}))
            elif self.path == "/discover":
                result = service.catalog.discover(identity, data.get("query", ""), data.get("action"))
            elif self.path == "/catalog":
                result = service.catalog.status(identity)
                result["connected"] = service.client.is_connected
            elif self.path == "/schemas":
                if not identity.privileged:
                    raise PermissionError("完整 schema 只允许本机或主人私聊")
                result = service.catalog.schemas()
            elif self.path == "/action":
                result = service.call(identity, data["action"], data.get("params", {}),
                                      data.get("operation_id"), data.get("timeout", 30))
            elif self.path == "/operation":
                if not identity.privileged:
                    raise PermissionError("回执查询只允许本机或主人私聊")
                result = service.store.operation(data["operation_id"])
            elif self.path == "/events":
                with service.store.connection() as db:
                    rows = db.execute(
                        "SELECT * FROM events ORDER BY received DESC" if identity.privileged else
                        "SELECT * FROM events WHERE conversation=? ORDER BY received DESC",
                        () if identity.privileged else (identity.conversation,),
                    ).fetchall()
                result = []
                for row in rows:
                    event = json.loads(row["raw"])
                    if identity.privileged or service.store.identity(event).scope == identity.scope:
                        result.append({"event_id": row["id"], "event": event, "status": row["status"]})
                        if len(result) >= max(1, min(int(data.get("limit", 20)), 100)):
                            break
            else:
                return self.respond(404, {"error": "unknown endpoint"})
            self.respond(200, {"result": result})
        except PermissionError as exc:
            self.respond(403, {"error": str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            self.respond(400, {"error": str(exc)})
        except RuntimeError as exc:
            self.respond(409 if "StateConflict" in str(exc) else 503, {"error": str(exc)})
        except Exception:
            self.respond(503, {"error": "NapCat 能力入口暂不可用"})

    def respond(self, code, data):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
