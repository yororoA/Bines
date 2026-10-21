"""SQLite WAL shared memory, durable input queue and at-most-once send receipts."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from chatBot.config import DATA_DIR, OWNER_QQ_IDS
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
    def __init__(self, path=None, owners=None):
        self.path = path or prepare_gateway_database(DATA_DIR)
        self.owners = OWNER_QQ_IDS if owners is None else frozenset(map(str, owners))
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
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
            if "batch" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN batch TEXT")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
            if "memory_synced" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN memory_synced INTEGER DEFAULT 0")

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

    def claim(self, worker, debounce=1.5, max_wait=5, limit=12, lease=600):
        """Atomically claim one sender's batch; no two workers own the same conversation."""
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE events SET status='pending',worker=NULL WHERE status='working' AND lease<?", (now,))
            row = db.execute("""
                SELECT conversation,MIN(received) first,MAX(received) last FROM events
                WHERE status='pending' AND attempts<5 AND conversation NOT IN
                (SELECT conversation FROM events WHERE status='working')
                GROUP BY conversation HAVING MAX(received)<=? OR MIN(received)<=?
                ORDER BY first LIMIT 1
            """, (now-debounce, now-max_wait)).fetchone()
            if not row:
                return []
            rows = db.execute(
                "SELECT * FROM events WHERE status='pending' AND attempts<5 AND conversation=? ORDER BY received,id LIMIT ?",
                (row["conversation"], limit),
            ).fetchall()
            batch = rows[0]["batch"]
            if batch:
                rows = db.execute("SELECT * FROM events WHERE batch=? AND status='pending' ORDER BY received,id", (batch,)).fetchall()
            else:
                rows = [event for event in rows if not event["batch"]]
                batch = hashlib.sha256("|".join(event["id"] for event in rows).encode()).hexdigest()
            for event in rows:
                db.execute("UPDATE events SET status='working',worker=?,lease=?,attempts=attempts+1,batch=? WHERE id=?",
                           (worker, now+lease, batch, event["id"]))
            return [{**dict(row), "batch": batch} for row in rows]

    def renew(self, ids, worker, lease=600):
        with self.connection() as db:
            for event_id in ids:
                db.execute("UPDATE events SET lease=? WHERE id=? AND worker=? AND status='working'",
                           (time.time()+lease, event_id, worker))

    def finish(self, ids, worker, error=None):
        with self.connection() as db:
            for event_id in ids:
                db.execute("UPDATE events SET status=?,error=?,worker=NULL WHERE id=? AND worker=?",
                           ("failed" if error else "done", error, event_id, worker))

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

    def save_turn(self, turn_id, status, payload=None):
        with self.connection() as db:
            db.execute("UPDATE turns SET status=?,payload=? WHERE id=?",
                       (status, json.dumps(payload, ensure_ascii=False) if payload else None, turn_id))
