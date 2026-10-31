"""SQLite WAL shared memory, durable input queue and at-most-once send receipts."""
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from chatBot.config import GATEWAY_DIR, OWNER_QQ_IDS
from chatBot.messages import Identity, readable


def prepare_gateway_database(data_dir):
    """Move the former NapCat database before the new Memory Server opens memory.db."""
    directory = Path(data_dir)
    legacy, target = directory / "memory.db", directory / "gateway.db"
    if not legacy.exists():
        return target
    with sqlite3.connect(str(legacy), timeout=15) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if "conversation" not in columns or "external_id" not in columns:
            return target  # This is already a MemoryRepository database.
        if target.exists():
            raise RuntimeError("旧 memory.db 和 gateway.db 同时存在，请先合并旧网关数据")
        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0]:
            raise RuntimeError("旧 QQ 数据库仍在使用，请停止原服务后再启动")
    legacy.rename(target)
    for suffix in ("-wal", "-shm"):
        source = Path(str(legacy) + suffix)
        if source.exists():
            source.rename(Path(str(target) + suffix))
    return target


class MemoryStore:
    def __init__(self, path=None, owners=None, *, max_attempts=None, retry_base=None, retry_max=None):
        self.path = path or prepare_gateway_database(GATEWAY_DIR)
        self.owners = OWNER_QQ_IDS if owners is None else frozenset(map(str, owners))
        self.max_attempts = int(max_attempts if max_attempts is not None else os.environ.get("BINES_QQ_MAX_ATTEMPTS", "5"))
        self.retry_base = float(retry_base if retry_base is not None else os.environ.get("BINES_QQ_RETRY_BASE_SECONDS", "2"))
        self.retry_max = float(retry_max if retry_max is not None else os.environ.get("BINES_QQ_RETRY_MAX_SECONDS", "60"))
        if (self.max_attempts < 1 or not math.isfinite(self.retry_base) or not math.isfinite(self.retry_max)
                or self.retry_base <= 0 or self.retry_max < self.retry_base):
            raise ValueError("QQ 重试次数必须为正数，退避间隔必须为有限正数且最大值不小于初始值")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, conversation TEXT NOT NULL,
                    actor TEXT NOT NULL, name TEXT, role TEXT NOT NULL, content TEXT NOT NULL,
                    raw TEXT, external_id TEXT, ts REAL, recalled INTEGER DEFAULT 0,
                    UNIQUE(scope, external_id)
                );
                CREATE INDEX IF NOT EXISTS message_scope ON messages(scope, ts);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, conversation TEXT NOT NULL, raw TEXT NOT NULL,
                    received REAL, status TEXT DEFAULT 'pending', lease REAL DEFAULT 0,
                    worker TEXT, attempts INTEGER DEFAULT 0, error TEXT, batch TEXT
                );
                CREATE INDEX IF NOT EXISTS event_queue ON events(status, received);
                CREATE TABLE IF NOT EXISTS state (
                    scope TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL, params TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, created REAL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, collection TEXT NOT NULL,
                    content TEXT NOT NULL, metadata TEXT NOT NULL, ts REAL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT
                );
                CREATE TABLE IF NOT EXISTS proactive_sessions (
                    conversation TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_runs (
                    id TEXT PRIMARY KEY, conversation TEXT NOT NULL, candidate TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', delivered_at REAL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS activity_handoffs (
                    turn_id TEXT PRIMARY KEY, conversation TEXT NOT NULL,
                    checkpoint TEXT NOT NULL, created REAL NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS worker_heartbeats (
                    worker TEXT PRIMARY KEY, component TEXT NOT NULL, updated REAL NOT NULL,
                    state TEXT NOT NULL
                );
            """)
            # Gateway and thinking workers may initialize the same old database.
            # Inspect and alter its schema under one lock to avoid duplicate ADDs.
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
            if "batch" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN batch TEXT")
            for name, definition in (("next_attempt", "REAL NOT NULL DEFAULT 0"),
                                     ("last_error", "TEXT"), ("failure_reason", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS event_due ON events(status,next_attempt,received)")
            db.execute("CREATE INDEX IF NOT EXISTS event_batch ON events(batch)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
            if "memory_synced" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN memory_synced INTEGER DEFAULT 0")
        from chatBot.recovery import initialize_recovery
        initialize_recovery(self)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def identity(self, event):
        return Identity.from_event(event, self.owners)

    def ingest(self, event):
        identity = self.identity(event)
        raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
        if event.get("post_type") in {"message", "message_sent"} and event.get("message_id") is not None:
            key = f"message:{identity.account}:{identity.group or 'p:'+identity.user}:{event['message_id']}"
        else:
            key = hashlib.sha256(raw.encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO events(id,conversation,raw,received) VALUES(?,?,?,?)",
                (key, identity.conversation, raw, now),
            ).rowcount
            if not inserted:
                return False
            if event.get("post_type") in {"message", "message_sent"}:
                self_message = str(event.get("user_id")) == identity.account or event.get("post_type") == "message_sent"
                ambiguous_self_private = self_message and not identity.group and identity.user == identity.account
                if not ambiguous_self_private:
                    db.execute(
                        """INSERT OR IGNORE INTO messages
                        (scope,conversation,actor,name,role,content,raw,external_id,ts)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (identity.scope, identity.conversation, identity.user, identity.name,
                         "assistant" if self_message else "user",
                         readable(event.get("message", event.get("raw_message", ""))), raw,
                         f"{identity.account}:{identity.group or 'p:'+identity.user}:{event.get('message_id', key)}",
                         float(event.get("time") or now)),
                    )
            if event.get("notice_type") in {"group_recall", "friend_recall"}:
                group = str(event.get("group_id") or "")
                user = str(event.get("user_id") or "")
                external = f"{identity.account}:{group or 'p:'+user}:{event.get('message_id')}"
                db.execute("UPDATE messages SET recalled=1,content='[消息已撤回]',raw=NULL,memory_synced=0 WHERE external_id=?", (external,))
        return True

    def claim(self, worker, debounce=1.5, max_wait=5, limit=12, lease=120):
        """Atomically claim one sender's batch; no two workers own the same conversation."""
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT * FROM events WHERE status='working' AND lease<?", (now,)).fetchall()
            for event in expired:
                turn_id = self._event_turn(event)
                completed = db.execute("SELECT 1 FROM turns WHERE id=? AND status='done'", (turn_id,)).fetchone()
                if completed and not self._turn_unresolved(db, turn_id):
                    db.execute("UPDATE events SET status='done',worker=NULL,lease=0,next_attempt=0,error=NULL,"
                               "failure_reason=NULL WHERE id=?", (event["id"],))
                    continue
                reason = "side_effects" if self._turn_writes(db, turn_id) else (
                    "retry_exhausted" if event["attempts"] >= self.max_attempts else None)
                db.execute("UPDATE events SET status=?,worker=NULL,lease=0,next_attempt=0,"
                           "error=?,last_error=?,failure_reason=? WHERE id=?",
                           ("failed" if reason else "pending", "处理租约已过期", "处理租约已过期", reason, event["id"]))
            # Older versions left exhausted pending rows invisible to claim().
            db.execute("UPDATE events SET status='failed',failure_reason='retry_exhausted',"
                       "last_error=COALESCE(last_error,error,'重试次数已耗尽') "
                       "WHERE status='pending' AND attempts>=?", (self.max_attempts,))
            row = db.execute("""
                SELECT conversation,MIN(received) first,MAX(received) last FROM events
                WHERE status='pending' AND attempts<? AND next_attempt<=? AND conversation NOT IN
                (SELECT conversation FROM events WHERE status='working')
                AND conversation NOT IN (SELECT conversation FROM events WHERE status='pending' AND next_attempt>?)
                GROUP BY conversation HAVING MAX(received)<=? OR MIN(received)<=?
                ORDER BY first LIMIT 1
            """, (self.max_attempts, now, now, now-debounce, now-max_wait)).fetchone()
            if not row:
                return []
            rows = db.execute(
                "SELECT * FROM events WHERE status='pending' AND attempts<? AND next_attempt<=? "
                "AND conversation=? ORDER BY received,id LIMIT ?",
                (self.max_attempts, now, row["conversation"], limit),
            ).fetchall()
            batch = rows[0]["batch"]
            if batch:
                rows = db.execute("SELECT * FROM events WHERE batch=? AND status='pending' ORDER BY received,id", (batch,)).fetchall()
            else:
                rows = [event for event in rows if not event["batch"]]
                # Internal initiations never coalesce into a real person's reply batch.
                humans = [event for event in rows
                          if json.loads(event["raw"]).get("post_type") != "bines_proactive"]
                rows = humans or rows[:1]
                batch = hashlib.sha256("|".join(event["id"] for event in rows).encode()).hexdigest()
            for event in rows:
                db.execute("UPDATE events SET status='working',worker=?,lease=?,attempts=attempts+1,batch=?,next_attempt=0 WHERE id=?",
                           (worker, now+lease, batch, event["id"]))
            return [{**dict(row), "batch": batch, "attempts": row["attempts"] + 1,
                     "worker": worker, "lease": now + lease} for row in rows]

    def check_lease(self, claims):
        """Fence actual actions by worker, attempt and a still-valid persisted lease."""
        now = time.time()
        with self.connection() as db:
            for claim in claims:
                row = db.execute("SELECT worker,attempts,status,lease FROM events WHERE id=?", (claim["id"],)).fetchone()
                if (not row or not claim.get("worker") or row["worker"] != claim["worker"]
                        or row["attempts"] != claim["attempts"] or row["status"] != "working"
                        or row["lease"] <= now):
                    return "qq_lease_lost"
        return ""

    def renew(self, ids, worker, lease=120):
        with self.connection() as db:
            for event_id in ids:
                db.execute("UPDATE events SET lease=? WHERE id=? AND worker=? AND status='working'",
                           (time.time()+lease, event_id, worker))

    def finish(self, ids, worker, error=None, *, retryable=False):
        """Retry only pre-side-effect failures; keep the last failure after recovery."""
        now = time.time()
        outcomes = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for event_id in ids:
                row = db.execute("SELECT * FROM events WHERE id=? AND worker=? AND status='working'",
                                 (event_id, worker)).fetchone()
                if not row:
                    continue  # A stale worker cannot finish another worker's lease.
                status, reason, due = "done", None, 0
                if error is not None:
                    reason = "side_effects" if self._turn_writes(db, self._event_turn(row)) else (
                        "non_retryable" if not retryable else "retry_exhausted" if row["attempts"] >= self.max_attempts else None)
                    status = "failed" if reason else "pending"
                    if not reason:
                        due = now + min(self.retry_max, self.retry_base * 2 ** min(row["attempts"] - 1, 20))
                db.execute("UPDATE events SET status=?,error=?,last_error=COALESCE(?,last_error),"
                           "failure_reason=?,worker=NULL,lease=0,next_attempt=? WHERE id=? AND worker=?",
                           (status, str(error)[:2000] if error is not None else None,
                            str(error)[:2000] if error is not None else None,
                            reason, due, event_id, worker))
                outcomes.append({"status": status, "failure_reason": reason, "next_attempt": due})
        return outcomes

    @staticmethod
    def _event_turn(row):
        event = json.loads(row["raw"])
        return row["id"] if event.get("post_type") == "bines_proactive" else row["batch"] or row["id"]

    def heartbeat(self, worker, state="idle", component="qq-thinking"):
        now = time.time()
        with self.connection() as db:
            db.execute("INSERT INTO worker_heartbeats VALUES(?,?,?,?) ON CONFLICT(worker) DO UPDATE SET "
                       "component=excluded.component,updated=excluded.updated,state=excluded.state",
                       (worker, component, now, state))
            db.execute("DELETE FROM worker_heartbeats WHERE updated<?", (now - 86400,))

    def health(self, stale_after=45):
        """Aggregate health only: never expose event bodies or user identifiers."""
        now = time.time()
        with self.connection() as db:
            states = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM events GROUP BY status")}
            due = db.execute("SELECT COUNT(*) FROM events WHERE status='pending' AND next_attempt<=?", (now,)).fetchone()[0]
            errors = {row["failure_reason"] or "legacy": row["n"] for row in db.execute(
                "SELECT failure_reason,COUNT(*) n FROM events WHERE status='failed' GROUP BY failure_reason")}
            workers = [dict(row) for row in db.execute("SELECT component,state,updated FROM worker_heartbeats")]
            oldest = db.execute("SELECT MIN(received) FROM events WHERE status IN ('pending','working')").fetchone()[0]
            recovery = db.execute("SELECT COUNT(*) FROM memory_recovery_queue").fetchone()[0]
        live = [row for row in workers if row["state"] in {"idle", "processing"} and now - row["updated"] <= stale_after]
        return {"events": states, "pending_due": due, "pending_delayed": states.get("pending", 0) - due,
                "dead_letters": states.get("failed", 0), "dead_letter_reasons": errors,
                "oldest_unfinished_seconds": max(0, now - oldest) if oldest is not None else 0,
                "workers_live": len(live), "workers_processing": sum(row["state"] == "processing" for row in live),
                "recovery_pending": recovery}

    def context(self, identity, limit=40, include_ids=False):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM messages WHERE scope=? ORDER BY ts DESC,id DESC LIMIT ?",
                              (identity.scope, limit)).fetchall()
        result = []
        for row in reversed(rows):
            content = row["content"]
            if identity.group:
                content = f"[QQ:{row['actor']} {row['name'] or ''}] {content}"
            if include_ids and not row["recalled"] and row["external_id"]:
                prefix = f"{identity.account}:{identity.group or 'p:'+identity.user}:"
                if row["external_id"].startswith(prefix):
                    content = f"[message_id={row['external_id'][len(prefix):]}] {content}"
            result.append({"role": row["role"], "content": content})
        return result

    def recall(self, identity, query, limit=5):
        """Recall older records within the same visibility boundary, without model downloads."""
        terms = {query[i:i+2].lower() for i in range(max(1, len(query)-1)) if query[i:i+2].strip()}
        with self.connection() as db:
            rows = db.execute(
                "SELECT actor,name,role,content FROM messages WHERE scope=? AND recalled=0 ORDER BY ts DESC LIMIT 2000",
                (identity.scope,),
            ).fetchall()
        ranked = []
        for row in rows[40:]:
            score = sum(term in row["content"].lower() for term in terms)
            if score:
                ranked.append((score, f"[QQ:{row['actor']} {row['name']}] {row['content']}" if identity.group else row["content"]))
        return [text for _, text in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]

    def add(self, identity, role, content, external_id=None, raw=None):
        with self.connection() as db:
            db.execute("""INSERT OR IGNORE INTO messages
                (scope,conversation,actor,name,role,content,external_id,raw,ts)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (identity.scope, identity.conversation,
                 identity.account if role == "assistant" and not identity.local else identity.user,
                 "Bines" if role == "assistant" else identity.name, role, content,
                 external_id or str(uuid.uuid4()), json.dumps(raw) if raw else None, time.time()))

    def state(self, identity):
        with self.connection() as db:
            row = db.execute("SELECT value FROM state WHERE scope=?", (identity.state_scope,)).fetchone()
        return json.loads(row[0]) if row else {}

    def update_state(self, identity, changes, expected_version=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM state WHERE scope=?", (identity.state_scope,)).fetchone()
            state = json.loads(row[0]) if row else {}
            version = state.get("_version", 0)
            if expected_version is not None and expected_version != version:
                raise ValueError("状态已被其他线程更新，请重新读取后再操作")
            state.update(changes)
            state["_version"] = version + 1
            db.execute("INSERT OR REPLACE INTO state(scope,value) VALUES(?,?)",
                       (identity.state_scope, json.dumps(state, ensure_ascii=False)))
        return state

    def begin_operation(self, operation_id, action, params):
        encoded = json.dumps(params, ensure_ascii=False, sort_keys=True)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row:
                if row["action"] != action or row["params"] != encoded:
                    raise ValueError("operation_id 已用于不同的动作或参数")
                return False, json.loads(row["result"]) if row["result"] else {
                    "status": "uncertain", "msg": "请求已开始，不能自动重发", "operation_id": operation_id,
                }
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)",
                       (operation_id, action, encoded, "started", None, time.time()))
        return True, None

    def complete_operation(self, operation_id, result):
        with self.connection() as db:
            db.execute("UPDATE operations SET status=?,result=? WHERE id=?",
                       (result.get("status", "uncertain"), json.dumps(result, ensure_ascii=False), operation_id))

    def operation(self, operation_id):
        with self.connection() as db:
            row = db.execute("SELECT status,result FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not row:
            return {"status": "not_found"}
        return json.loads(row["result"]) if row["result"] else {"status": "uncertain"}

    def begin_turn(self, turn_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if row:
                return dict(row)
            db.execute("INSERT INTO turns VALUES(?,'started',NULL)", (turn_id,))
        return None

    def save_turn(self, turn_id, status, payload=None, *, handoff=None):
        with self.connection() as db:
            db.execute("UPDATE turns SET status=?,payload=COALESCE(?,payload) WHERE id=?",
                       (status, json.dumps(payload, ensure_ascii=False) if payload is not None else None, turn_id))
            if handoff:
                db.execute(
                    "INSERT OR IGNORE INTO activity_handoffs VALUES(?,?,?,?,0)",
                    (turn_id, handoff["conversation"], json.dumps(handoff["checkpoint"], ensure_ascii=False),
                     time.time()),
                )

    def current_activity(self, conversation):
        """Include a finished owner turn that the scheduler has not consumed yet."""
        with self.connection() as db:
            row = db.execute(
                "SELECT checkpoint FROM activity_handoffs WHERE conversation=? AND consumed=0 "
                "ORDER BY created DESC LIMIT 1", (conversation,),
            ).fetchone()
            if row:
                checkpoint = json.loads(row["checkpoint"])
                return {} if checkpoint.get("mode") == "finish" else checkpoint
            row = db.execute("SELECT value FROM proactive_sessions WHERE conversation=?", (conversation,)).fetchone()
            state = json.loads(row["value"]) if row else {}
            pending = state.get("pending") or {}
            turn = db.execute("SELECT payload FROM turns WHERE id=? AND status='done'",
                              (pending.get("id"),)).fetchone() if pending else None
            checkpoint = json.loads(turn["payload"] or "{}").get("activity") if turn else None
            if checkpoint:
                if checkpoint.get("mode") == "finish":
                    return {}
                return {**checkpoint, "kind": pending["kind"], "topic": pending["topic"],
                        "reason": pending["reason"], "contact_allowed": pending.get("contact_allowed", True)}
        return state.get("activity") or {}

    def drop_turn(self, turn_id):
        """Forget an interrupted turn that never dispatched a write."""
        with self.connection() as db:
            db.execute("DELETE FROM turns WHERE id=? AND status='started'", (turn_id,))

    def turn_writes(self, turn_id):
        """Also account for journaled browser/MCP actions outside the QQ outbox."""
        with self.connection() as db:
            return self._turn_writes(db, turn_id)

    @staticmethod
    def _turn_unresolved(db, turn_id):
        if db.execute("SELECT 1 FROM operations WHERE id>=? AND id<? "
                      "AND status IN ('started','uncertain') LIMIT 1",
                      (turn_id + ":", turn_id + ";")).fetchone():
            return True
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_outbox'").fetchone():
            return False
        return bool(db.execute("SELECT 1 FROM memory_outbox WHERE method='record_experience' "
                    "AND json_extract(params,'$.record.turn_id')=? AND ("
                    "(json_extract(params,'$.record.category')='turn' AND "
                    "json_extract(params,'$.record.status') IN ('failed','uncertain','interrupted')) OR "
                    "(json_extract(params,'$.record.category')='tool' AND "
                    "json_extract(params,'$.record.status') IN ('started','uncertain'))) LIMIT 1",
                    (turn_id,)).fetchone())

    @staticmethod
    def _turn_writes(db, turn_id):
        row = db.execute("SELECT COUNT(*) FROM operations WHERE id>=? AND id<?",
                         (turn_id + ":", turn_id + ";")).fetchone()
        count = int(row[0])
        journal = db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_outbox'").fetchone()
        if journal:
            count += db.execute(
                "SELECT COUNT(*) FROM memory_outbox WHERE method='record_experience' "
                "AND json_extract(params,'$.record.turn_id')=? "
                "AND json_extract(params,'$.record.category')='tool'", (turn_id,),
            ).fetchone()[0]
        return count
