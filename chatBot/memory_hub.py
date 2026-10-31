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
from common.memory_journal import MemoryOutbox, OutboxDependencyPending


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
        self.outbox = MemoryOutbox(store.path)
        with store.connection() as db:
            db.execute("CREATE INDEX IF NOT EXISTS gateway_memory_pending ON messages(memory_synced,ts,id)")
        profile = profile or os.environ.get("_BINES_LAUNCH_PROFILE") or os.environ.get("BINES_PROFILE", "full")
        backend = os.environ.get("BINES_MEMORY_BACKEND", "auto").strip().lower()
        if self.service is None and client is not None:
            self.client = client
        elif self.service is None:
            from config import ZMQ_HOST, ZMQ_PORTS
            remote = MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"])
            # Docker compose sets remote explicitly: never open the shared memory.db
            # a second time just because the Memory Server is still loading its model.
            if backend == "remote":
                self.client = remote
            elif backend == "embedded":
                self._embedded(settings)
            elif profile != "chatbot" or (settings is None and remote.ping(timeout_ms=3000)):
                self.client = remote
            else:
                self._embedded(settings)

    def _embedded(self, settings=None):
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
            # Only the embedded repository needs serialization. Never hold this
            # lock across a batch of network calls or unrelated conversations.
            with self.lock:
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

    def remember_receipt(self, identity, action, params, result):
        """Persist sends at the gateway, before the Thinking process receives its receipt."""
        from chatBot.messages import SEND_ACTIONS, readable, sent_message
        if action not in SEND_ACTIONS or not isinstance(result, dict) or result.get("status") != "ok":
            return
        message_id = (result.get("data") or {}).get("message_id")
        if message_id is None or result.get("retcode", 0) not in (0, "0"):
            return
        account = identity.account
        if identity.local:
            with self.store.connection() as db:
                recent = db.execute("SELECT raw FROM messages WHERE role='user' AND raw IS NOT NULL "
                                    "ORDER BY ts DESC LIMIT 1").fetchone()
            account = str(json.loads(recent["raw"]).get("self_id") or "") if recent else ""
            if not account:
                return
        group = str(params.get("group_id") or "")
        user = str(params.get("user_id") or identity.user)
        target = Identity(account, user, group, owner=user in self.store.owners)
        self.store.add(target, "assistant", readable(sent_message(params)),
            external_id=f"{account}:{group or 'p:'+user}:{message_id}",
            raw={"message": sent_message(params), "receipt": result})

    def backfill_experiences(self, limit=100):
        """Recover one bounded batch; durable cursors and triggers resume later."""
        from chatBot.recovery import backfill
        with self.lock:
            return backfill(self, limit)

    def _sync_outbox_record(self, method, **params):
        if method == "record_message":
            result = self._call(method, **params)
            self._ack_message(params)
            return result
        if method == "record_experience":
            envelope = params["envelope"]
            account = str(envelope.get("external_account_id") or "")
            user = str(envelope.get("external_user_id") or "")
            group = str(envelope.get("external_group_id") or "")
            if account:
                identity = Identity(account, user, group, owner=user in self.store.owners)
                record = dict(params["record"])
                refs = list(record.get("source_message_refs") or [])
                message_ids = set()
                correction_ids = set()
                def collect(value):
                    if isinstance(value, dict):
                        for key, item in value.items():
                            if key in {"message_id", "reply_to"} and isinstance(item, (str, int)):
                                message_ids.add(str(item))
                            if value.get("type") in {"reply", "node"} and key == "data" and isinstance(item, dict):
                                if item.get("id") is not None:
                                    message_ids.add(str(item["id"]))
                            collect(item)
                    elif isinstance(value, list):
                        for item in value:
                            collect(item)
                collect(record.get("args") or {})
                pending = {}
                with self.store.connection() as db:
                    for event_id in record.get("event_ids") or []:
                        event = db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()
                        if event:
                            data = json.loads(event["raw"])
                            if data.get("post_type") == "message" and data.get("message_id") is not None:
                                message_ids.add(str(data["message_id"]))
                                if identity.privileged and str(data.get("user_id")) == identity.user:
                                    correction_ids.add(str(data["message_id"]))
                    correction_refs = []
                    for message_id in message_ids:
                        external = f"{account}:{group or 'p:'+user}:{message_id}"
                        message = db.execute("SELECT * FROM messages WHERE scope=? AND external_id=?",
                                             (identity.scope, external)).fetchone()
                        if message:
                            refs.append(self.source + ":" + str(message["id"]))
                            if not message["memory_synced"]:
                                pending[message["id"]] = dict(message)
                            if message_id in correction_ids:
                                correction_refs.append(self.source + ":" + str(message["id"]))
                    for ref in refs:
                        if isinstance(ref, str) and ref.startswith(self.source + ":"):
                            message = db.execute("SELECT * FROM messages WHERE id=? AND scope=? AND memory_synced=0",
                                                 (ref[len(self.source) + 1:], identity.scope)).fetchone()
                            if message:
                                pending[message["id"]] = dict(message)
                if pending:
                    for message in pending.values():
                        self._stage_message(message)
                    raise OutboxDependencyPending([self.source + ":" + str(mid) for mid in pending])
                params["record"] = {**record, "source_message_refs": list(dict.fromkeys(refs)),
                                    "correction_source_refs": correction_refs}
        return self._call(method, **params)

    def _ack_message(self, params):
        ref = params.get("source_ref", "")
        if not ref.startswith(self.source + ":"):
            return
        with self.store.connection() as db:
            db.execute("UPDATE messages SET memory_synced=1 WHERE id=? AND content=? AND recalled=?",
                       (ref[len(self.source) + 1:], params.get("content"), bool(params.get("recalled"))))

    def _stage_message(self, row):
        with self.store.connection() as db:
            event_row = db.execute("SELECT batch FROM events WHERE id=?",
                                   ("message:" + (row["external_id"] or ""),)).fetchone()
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
            parts = (row["external_id"] or "").split(":")
            identity = (Identity(parts[0], parts[1][2:], owner=True, name=row["name"] or "")
                        if len(parts) >= 3 and parts[1].startswith("p:") else LOCAL_IDENTITY)
        ref = self.source + ":" + str(row["id"])
        params = dict(envelope=identity_envelope(identity, row["content"], row["ts"]),
                      role=row["role"], content=row["content"], recalled=bool(row["recalled"]),
                      state_update=event_row is not None)
        if event_row and event_row["batch"]:
            params["turn_ref"] = ("experience:" + hashlib.sha256(str(self.outbox.path.resolve()).encode()).hexdigest()
                                  + ":" + event_row["batch"] + ":turn")
        self.outbox.put(ref, "record_message", params, if_changed=True)
        with self.store.connection() as db:
            done = db.execute("SELECT params FROM memory_outbox WHERE source_ref=? AND synced=1", (ref,)).fetchone()
        if done:
            self._ack_message(json.loads(done["params"]))
        return ref

    def sync(self):
        """Stage one bounded batch; each source is acknowledged only after central commit."""
        with self.store.connection() as db:
            rows = db.execute("""SELECT m.* FROM messages m LEFT JOIN memory_outbox o ON o.source_ref=? || m.id
                WHERE m.memory_synced=0 AND (o.source_ref IS NULL OR o.synced=1
                    OR CASE WHEN json_valid(o.params) THEN
                        json_extract(o.params,'$.content') IS NOT m.content
                        OR json_extract(o.params,'$.recalled') IS NOT m.recalled ELSE 1 END)
                ORDER BY m.ts,m.id LIMIT 500""", (self.source + ":",)).fetchall()
        for row in rows:
            self._stage_message(row)
        return max(len(rows), self.outbox.sync(self._sync_outbox_record))

    def _sync_context(self, identity, event_id=None):
        """A finite conversation snapshot, independent of other conversations' backlog."""
        import time
        from memory.client import MemoryUnavailable
        window = max(40, getattr(getattr(self.service, "settings", None), "working_window", 30))
        with self.store.connection() as db:
            rows = list(db.execute("SELECT * FROM messages WHERE conversation=? ORDER BY ts DESC,id DESC LIMIT ?",
                                   (identity.conversation, window)).fetchall())
            # Withdrawal of old evidence in the visible scope is also a required
            # watermark, even when the original message is outside the window.
            recalled = db.execute("SELECT * FROM messages WHERE scope=? AND memory_synced=0 AND recalled=1 "
                                  "ORDER BY id LIMIT 501", (identity.scope,)).fetchall()
            if len(recalled) > 500:
                raise MemoryUnavailable("当前作用域撤回记录积压，等待后台同步")
            rows.extend(recalled)
            if event_id:
                event = db.execute("SELECT batch FROM events WHERE id=? AND conversation=?",
                                   (event_id, identity.conversation)).fetchone()
                if event:
                    events = db.execute("SELECT id FROM events WHERE (id=? OR batch=?) AND conversation=?",
                                        (event_id, event["batch"], identity.conversation)).fetchall()
                    for current in events:
                        row = db.execute("SELECT * FROM messages WHERE conversation=? AND external_id=?",
                                         (identity.conversation, current["id"].removeprefix("message:"))).fetchone()
                        if row:
                            rows.append(row)
        pending = {row["id"]: row for row in rows if not row["memory_synced"]}
        refs = [self._stage_message(row) for row in pending.values()]
        deadline = time.monotonic() + 5
        while refs:
            self.outbox.sync(self._sync_outbox_record, source_refs=refs)
            with self.store.connection() as db:
                remaining = db.execute("SELECT o.source_ref,d.attempts FROM memory_outbox o "
                    "LEFT JOIN memory_outbox_delivery d ON d.source_ref=o.source_ref AND d.revision=o.revision "
                    "WHERE o.synced=0 AND o.source_ref IN (" + ",".join("?" for _ in refs) + ")", refs).fetchall()
            if not remaining:
                return
            if any(row["attempts"] for row in remaining) or time.monotonic() >= deadline or self.stop.is_set():
                raise MemoryUnavailable("当前会话原话正在等待记忆同步恢复")
            self.stop.wait(0.02)  # Another consumer may already own these exact records.

    def call(self, identity, method, params, event_id=None):
        params = dict(params)
        if not identity.local:
            if method == "build_context":
                params["envelope"] = identity_envelope(identity)
            elif method in {"get_state", "put_state"}:
                # A group owner still writes only their own state within that group.
                params["scope"] = identity.state_scope
            elif method == "life_snapshot":
                params["scope"] = identity.scope
            elif method == "regulate_life":
                if not identity.privileged or not event_id:
                    raise PermissionError("生命周期自我调节仅允许本机或主人私聊的可信轮次")
                params = {
                    "scope": identity.scope,
                    "mode": params.get("mode"),
                    "duration_minutes": params.get("duration_minutes", 0),
                    "reason": params.get("reason", ""),
                    "source_ref": f"{self.source}:life-regulation:{event_id}",
                }
            elif method in {"search", "search_with_trace"}:
                params["scopes"] = [identity.scope]
                params["k"] = max(1, min(int(params.get("k", 8)), 20))
            elif method == "read_memory_source":
                params = {"source_id": params.get("source_id"), "scope": identity.scope,
                          "offset": params.get("offset", 0), "limit": params.get("limit", 1024)}
            elif method == "activity_snapshot" and identity.owner and not identity.group and event_id:
                # Only a persisted scheduler event can recheck cross-channel activity.
                with self.store.connection() as db:
                    event_row = db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()
                if not event_row or json.loads(event_row["raw"]).get("post_type") != "bines_proactive":
                    raise PermissionError("活动检查仅用于可信自主轮次")
                params = {"limit": 0}
            else:
                raise PermissionError("当前 QQ 会话只允许读取自身上下文和更新自身状态")
        if method == "build_context":
            self._sync_context(identity, event_id)
        if method == "put_state" and not identity.local:
            # 服务端从事件推导消息来源，模型不能指定要跳过哪个状态任务。
            params.pop("source_ref", None)
            if event_id:
                self._sync_context(identity, event_id)
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
        import time
        logging.getLogger("bines.memory.gateway").info(
            "记忆后端 · %s", "网关内置词面检索" if self.service is not None else "Memory Server (RAG)"
        )
        if self.service is None:
            timeout = float(os.environ.get("BINES_MEMORY_READY_TIMEOUT", "300"))
            deadline = time.monotonic() + timeout
            while True:
                try:
                    self._call("ping")
                    break
                except Exception as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Memory Server 未就绪（等待 {timeout:.0f}s）：{exc}") from exc
                    logging.getLogger("bines.memory.gateway").warning(
                        "Memory Server 尚未就绪，2 秒后重试：%s", exc,
                    )
                    time.sleep(2)
        else:
            self._call("ping")
        self.import_legacy()
        self.backfill_experiences()
        self.sync()
        def consume():
            import logging
            while not self.stop.wait(0.5):
                try:
                    self.backfill_experiences()
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
