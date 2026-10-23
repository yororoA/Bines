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
from common.memory_journal import MemoryOutbox


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

    def backfill_experiences(self):
        """Recover saved autonomous outcomes and durable native operation receipts."""
        from common.memory_journal import MemoryJournal, outcome_status
        count = 0
        with self.store.connection() as db:
            events = [dict(row) for row in db.execute("SELECT * FROM events")]
            turns = {row["id"]: dict(row) for row in db.execute("SELECT * FROM turns")}
            operations = [dict(row) for row in db.execute("SELECT * FROM operations ORDER BY created")]
        for event_row in events:
            event = json.loads(event_row["raw"])
            turn_id = event_row["batch"] or event_row["id"]
            proactive = event.get("post_type") == "bines_proactive"
            if proactive:
                turn_id = event_row["id"]
            journal = MemoryJournal(self.outbox, identity_envelope(self.store.identity(event)),
                                    turn_id, autonomous=proactive, candidate=event.get("proactive"),
                                    event_ids=[event_row["id"]])
            turn = turns.get(turn_id)
            payload = json.loads(turn["payload"] or "{}") if turn else {}
            items = list(payload.get("outcomes") or [])
            with self.store.connection() as db:
                already_journaled = db.execute(
                    "SELECT 1 FROM memory_outbox WHERE json_extract(params,'$.record.turn_id')=? "
                    "AND source_ref NOT LIKE '%:legacy:%' LIMIT 1", (turn_id,),
                ).fetchone()
            if already_journaled:
                items = []
            items.extend({"action": op["action"], "args": json.loads(op["params"]),
                          "result": json.loads(op["result"]) if op["result"] else "",
                          "status": outcome_status(json.loads(op["result"])) if op["result"] else "uncertain",
                          "started_at": op["created"], "legacy_operation": op["id"]}
                         for op in operations if op["id"].startswith(turn_id + ":"))
            if proactive and payload.get("reflection") and not already_journaled:
                items.append({"action": "autonomous_turn", "result": payload["reflection"], "status": "completed"})
            for index, item in enumerate(items):
                operation = item.get("legacy_operation")
                if operation:
                    self.remember_receipt(self.store.identity(event), item["action"], item["args"], item["result"])
                    with self.store.connection() as db:
                        if db.execute("SELECT 1 FROM memory_outbox WHERE source_ref=?",
                                      (journal.prefix + ":native:" + operation,)).fetchone():
                            continue
                key = journal.prefix + ":legacy:" + str(index)
                if operation:
                    key = journal.prefix + ":native:" + operation
                with self.store.connection() as db:
                    if db.execute("SELECT 1 FROM memory_outbox WHERE source_ref=?", (key,)).fetchone():
                        continue
                record = {**item, "category": "tool", "legacy": True,
                          "status": item.get("status") or outcome_status(item.get("result")),
                          "started_at": item.get("started_at") or event_row["received"],
                          "finished_at": item.get("started_at") or event_row["received"]}
                journal._record(key, record)
                count += 1
        return {"queued": count}

    def _sync_outbox_record(self, method, **params):
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
                with self.store.connection() as db:
                    for event_id in record.get("event_ids") or []:
                        event = db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()
                        if event:
                            data = json.loads(event["raw"])
                            if data.get("post_type") == "message" and data.get("message_id") is not None:
                                message_ids.add(str(data["message_id"]))
                    for message_id in message_ids:
                        external = f"{account}:{group or 'p:'+user}:{message_id}"
                        message = db.execute("SELECT id FROM messages WHERE scope=? AND external_id=?",
                                             (identity.scope, external)).fetchone()
                        if message:
                            refs.append(self.source + ":" + str(message["id"]))
                params["record"] = {**record, "source_message_refs": list(dict.fromkeys(refs))}
        return self._call(method, **params)

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
            if len(rows) == 500:
                return 500  # Source messages must commit before their observations.
            outbox_count = self.outbox.sync(self._sync_outbox_record)
            return max(len(rows), outbox_count)

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
