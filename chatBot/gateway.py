"""Authenticated loopback capability gateway. Only this process owns OneBot."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from chatBot.config import GATEWAY_HOST, GATEWAY_PORT, gateway_token
from chatBot.messages import LOCAL_IDENTITY
from common.local_auth import token_matches


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
        if not token_matches(self.headers.get("Authorization", ""), f"Bearer {self.server.token}"):
            return self.respond(401, {"error": "authentication required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4*1024*1024:
                raise ValueError("请求大小无效")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求必须是 JSON 对象")
            identity = LOCAL_IDENTITY
            event = {}
            event_id = data.get("event_id")
            if event_id:
                with self.server.service.store.connection() as db:
                    row = db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()
                if not row:
                    raise PermissionError("未知 QQ 事件")
                event = {**json.loads(row["raw"]), "event_id": event_id}
                identity = self.server.service.store.identity(event)
            service = self.server.service
            if self.path == "/health":
                result = {"connected": service.client.is_connected}
            elif self.path == "/memory":
                if self.server.memory_hub is None:
                    raise RuntimeError("Memory hub unavailable")
                result = self.server.memory_hub.call(identity, data["method"], data.get("params", {}), event_id=event_id)
            elif self.path == "/discover":
                result = service.catalog.discover(identity, data.get("query", ""), data.get("action"))
            elif self.path == "/catalog":
                result = service.catalog.status(identity)
                result["connected"] = service.client.is_connected
            elif self.path == "/schemas":
                if not (identity.local or identity.owner):
                    raise PermissionError("完整 schema 只允许本机或已绑定主人")
                result = service.catalog.schemas()
            elif self.path == "/action":
                if event.get("post_type") == "bines_proactive":
                    from chatBot.proactive import dispatch_allowed
                    activity = self.server.memory_hub.call(
                        LOCAL_IDENTITY, "activity_snapshot", {},
                    ).get("last_owner_activity") or {}
                    dispatch_allowed(service.store, event, data["action"], data.get("params", {}),
                                     data.get("operation_id"), central_activity=float(activity.get("ts") or 0))
                journal, entry = None, None
                if self.server.memory_hub and data["action"] not in {"get_friend_msg_history", "get_group_msg_history"}:
                    from common.memory_journal import MemoryJournal
                    from chatBot.memory_hub import identity_envelope
                    turn_id = data.get("operation_id") or event_id or "local-api"
                    if ":tool:" in turn_id:
                        turn_id = turn_id.split(":tool:", 1)[0]
                    elif ":reply:" in turn_id:
                        turn_id = turn_id.split(":reply:", 1)[0]
                    journal = MemoryJournal(self.server.memory_hub.outbox, identity_envelope(identity),
                        turn_id, autonomous=event.get("post_type") == "bines_proactive",
                        candidate=event.get("proactive"), event_ids=[event_id] if event_id else [])
                    operation_id = data.get("operation_id")
                    entry = journal.begin({"action": data["action"], "args": data.get("params", {})},
                        key=journal.prefix + ":native:" + operation_id if operation_id else None)
                try:
                    result = service.call(identity, data["action"], data.get("params", {}),
                                          data.get("operation_id"), data.get("timeout", 30))
                except BaseException as exc:
                    if journal:
                        journal.finish(entry, str(exc), status="uncertain")
                    raise
                if journal:
                    journal.finish(entry, result)
                    self.server.memory_hub.remember_receipt(identity, data["action"], data.get("params", {}), result)
            elif self.path == "/quoted_message":
                result = service.quoted_message(identity, event_id, data.get("message_id"))
            elif self.path == "/operation":
                if not (identity.local or identity.owner):
                    raise PermissionError("回执查询只允许本机或已绑定主人")
                result = service.store.operation(data["operation_id"])
            elif self.path == "/operation_state":
                # Internal endpoint for local Thinking processes: it only reads the
                # receipt table so a lost HTTP request can be retried without guessing.
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
