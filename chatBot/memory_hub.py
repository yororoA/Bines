"""Gateway bridge to the central memory service, embedded only in the QQ profile."""
import hashlib
import json
import os
import threading
from pathlib import Path

from chatBot.messages import Identity
from memory.channels import local_envelope, qq_envelope
from memory.client import MemoryClient
from memory.server import dispatch


def identity_envelope(identity, content="", timestamp=None):
    if identity.local:
        return local_envelope(content, timestamp=timestamp).to_dict()
    return qq_envelope({
        "self_id": identity.account, "user_id": identity.user,
        "group_id": identity.group, "sender_name": identity.name,
    }, content, timestamp).to_dict()


class MemoryHub:
    def __init__(self, store, profile=None, service=None, settings=None, client=None):
        self.store = store
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.service = service
        self.source = "gateway:" + hashlib.sha256(str(Path(store.path).resolve()).encode()).hexdigest()
        profile = profile or os.environ.get("_BINES_LAUNCH_PROFILE") or os.environ.get("BINES_PROFILE", "full")
        if self.service is None and client is not None:
            self.client = client
        elif self.service is None:
            from config import ZMQ_HOST, ZMQ_PORTS
            remote = MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"])
            # Vector RAG is the source of truth whenever the Memory Server is up;
            # the embedded lexical service only covers a standalone QQ host. An
            # explicit ``settings`` always means isolated embedded memory (tests).
            if profile != "chatbot" or (settings is None and remote.ping(timeout_ms=3000)):
                self.client = remote
            else:
                from memory.lexical_index import LexicalIndex
                from memory.repository import MemoryRepository
                from memory.service import MemoryService
                from memory.settings import load_settings
                settings = settings or load_settings()
                repo = MemoryRepository(settings.db_path)
                self.service = MemoryService(repo, LexicalIndex(repo), settings)
                self.service.sync_identities()

    def _call(self, method, **params):
        if self.service is not None:
            reply = dispatch(self.service, {"method": method, "params": params})
            if not reply["ok"]:
                raise RuntimeError(reply["error"])
            return reply.get("result")
        return self.client.call(method, **params)

    def import_legacy(self):
        with self.lock, self.store.connection() as db:
            states = [{"scope": row["scope"], "data": json.loads(row["value"])}
                      for row in db.execute("SELECT * FROM state")]
            memories = [{**dict(row), "metadata": json.loads(row["metadata"])}
                        for row in db.execute("SELECT * FROM memories")]
            return self._call("import_gateway_snapshot", source=self.source, states=states, memories=memories)

    def sync(self):
        """Acknowledge only after central commit; source_ref makes crash recovery safe."""
        with self.lock:
            with self.store.connection() as db:
                rows = db.execute(
                    "SELECT * FROM messages WHERE memory_synced=0 ORDER BY ts,id LIMIT 500"
                ).fetchall()
            for row in rows:
                with self.store.connection() as db:
                    live_event = db.execute(
                        "SELECT 1 FROM events WHERE id=?", ("message:" + (row["external_id"] or ""),),
                    ).fetchone() is not None
                scope = row["scope"]
                if scope.startswith("qq:"):
                    _, account, kind, target = scope.split(":", 3)
                    identity = Identity(account, target if kind == "private" else row["actor"],
                                        target if kind == "group" else "", row["name"] or "")
                elif row["conversation"] in {"local", "qq:local:p:u:owner"}:
                    from chatBot.messages import LOCAL_IDENTITY
                    identity = LOCAL_IDENTITY
                elif row["conversation"].startswith("qq:") and ":p:u:" in row["conversation"]:
                    account, user = row["conversation"][3:].split(":p:u:", 1)
                    identity = Identity(account, user, owner=True, name=row["name"] or "")
                else:
                    from chatBot.messages import LOCAL_IDENTITY
                    # Owner outgoing receipts lack raw self_id; recover the private peer from external_id.
                    parts = (row["external_id"] or "").split(":")
                    identity = (Identity(parts[0], parts[1][2:], owner=True, name=row["name"] or "")
                                if len(parts) >= 3 and parts[1].startswith("p:") else LOCAL_IDENTITY)
                self._call(
                    "record_message", envelope=identity_envelope(identity, row["content"], row["ts"]),
                    role=row["role"], content=row["content"],
                    source_ref=self.source + ":" + str(row["id"]), recalled=bool(row["recalled"]),
                    state_update=live_event,
                )
                with self.store.connection() as db:
                    db.execute("UPDATE messages SET memory_synced=1 WHERE id=? AND content=? AND recalled=?",
                               (row["id"], row["content"], row["recalled"]))
            return len(rows)

    def call(self, identity, method, params, event_id=None):
        with self.lock:
            params = dict(params)
            if not identity.local:
                if method == "build_context":
                    params["envelope"] = identity_envelope(identity)
                elif method in {"get_state", "put_state"}:
                    # A group owner still writes only their own state within that group.
                    params["scope"] = identity.state_scope
                elif method == "search":
                    params["scopes"] = [identity.scope]
                    params["k"] = max(1, min(int(params.get("k", 8)), 20))
                else:
                    raise PermissionError("当前 QQ 会话只允许读取自身上下文和更新自身状态")
            if method == "build_context":
                while self.sync() == 500:
                    pass
            if method == "put_state" and not identity.local:
                # 服务端从事件推导消息来源，模型不能指定要跳过哪个状态任务。
                params.pop("source_ref", None)
                if event_id:
                    while self.sync() == 500:
                        pass
                    with self.store.connection() as db:
                        event = json.loads(db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()[0])
                        external = f"{identity.account}:{identity.group or 'p:'+identity.user}:{event.get('message_id')}"
                        row = db.execute(
                            "SELECT id FROM messages WHERE scope=? AND external_id=?", (identity.scope, external),
                        ).fetchone()
                    if row:
                        params["source_ref"] = self.source + ":" + str(row["id"])
            return self._call(method, **params)

    def start(self):
        import logging
        logging.getLogger("bines.memory.gateway").info(
            "记忆后端 · %s", "网关内置词面检索" if self.service is not None else "Memory Server (RAG)"
        )
        self._call("ping")
        self.import_legacy()
        while self.sync() == 500:
            pass
        def consume():
            import logging
            while not self.stop.wait(0.5):
                try:
                    self.sync()
                except Exception as exc:
                    logging.getLogger("bines.memory.gateway").warning("记忆同步待重试：%s", exc)
                    self.stop.wait(5)
        self.thread = threading.Thread(target=consume, name="QQMemorySync", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        with self.lock:
            if self.service:
                self.service.repo.close()


class GatewayMemoryClient(MemoryClient):
    def __init__(self, gateway=None):
        from chatBot.gateway_client import GatewayClient
        self.gateway = gateway or GatewayClient()
        self.timeout_ms = 30000

    def call(self, method, timeout_ms=None, **params):
        from memory.client import MemoryUnavailable
        result = self.gateway.request(
            "/memory", {"method": method, "params": params}, (timeout_ms or self.timeout_ms) / 1000,
        )
        if isinstance(result, dict) and result.get("status") == "uncertain":
            raise MemoryUnavailable(result.get("msg", "Memory gateway unavailable"))
        return result
