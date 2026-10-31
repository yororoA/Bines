"""SQLite source of truth for all memory data. Only the Memory Server process writes to it."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence

from .models import ROLES, content_hash, is_system_event

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_links (
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    identity_id TEXT NOT NULL REFERENCES identities(id),
    PRIMARY KEY (channel, external_id)
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    scope TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_message_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    scope TEXT NOT NULL,
    role TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    actor_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    ts REAL NOT NULL,
    day_key TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'live',
    episode_id TEXT,
    source_ref TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages(conversation_id, episode_id);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    conversation_id TEXT,
    subject_id TEXT,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 5,
    start_ts REAL,
    end_ts REAL,
    day_key TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    meta TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'live',
    content_hash TEXT NOT NULL,
    source_ref TEXT UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    indexed_at REAL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, kind, deleted);
CREATE INDEX IF NOT EXISTS idx_memories_day ON memories(scope, kind, day_key);
CREATE TABLE IF NOT EXISTS state (
    scope TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    conversation_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    lease_until REAL,
    last_error TEXT,
    dedupe_key TEXT UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, available_at);
CREATE TABLE IF NOT EXISTS migration_ledger (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_table TEXT NOT NULL,
    target_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    migrated_at REAL NOT NULL,
    PRIMARY KEY (source, source_id)
);
"""

_MEMORY_COLUMNS = (
    "id, kind, scope, conversation_id, subject_id, content, importance, start_ts, end_ts, day_key, "
    "tags, meta, origin, content_hash, source_ref, created_at, updated_at, indexed_at, deleted"
)
_PENDING_MESSAGES = "episode_id IS NULL AND consolidation_candidate(role, content, origin)"


def consolidation_candidate(role: str, content: str, origin: str) -> bool:
    text = (content or "").strip()
    return (
        role in ROLES and bool(text) and not is_system_event(text)
        and text.lower() not in ("[消息已撤回]", "[message recalled]", "[message deleted]")
        and (origin or "").strip().lower() not in (
            "archived", "recalled", "withdrawn", "retracted", "deleted", "system",
        )
    )


class StateConflict(RuntimeError):
    pass


def _memory_row(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["tags"] = json.loads(item.get("tags") or "[]")
    item["meta"] = json.loads(item.get("meta") or "{}")
    item["deleted"] = bool(item.get("deleted"))
    return item


class MemoryRepository:
    def __init__(self, db_path: Path | str, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("consolidation_candidate", 3, consolidation_candidate, deterministic=True)
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(db_path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        job_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for column in ("started_at", "duration_seconds"):
            if column not in job_columns:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} REAL")
        if "lease_recoveries" not in job_columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN lease_recoveries INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ---------- identities ----------
    def upsert_identity(self, identity_id: str, kind: str, display_name: str = "") -> None:
        self.conn.execute(
            "INSERT INTO identities(id, kind, display_name, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET display_name = CASE WHEN excluded.display_name != '' "
            "THEN excluded.display_name ELSE identities.display_name END",
            (identity_id, kind, display_name, self.clock()),
        )

    def link_identity(self, channel: str, external_id: str, identity_id: str) -> None:
        self.conn.execute(
            "INSERT INTO identity_links(channel, external_id, identity_id) VALUES (?, ?, ?) "
            "ON CONFLICT(channel, external_id) DO UPDATE SET identity_id = excluded.identity_id",
            (channel, str(external_id), identity_id),
        )

    def unlink_identities(self, channel: str, identity_id: str, keep: Iterable[str]) -> None:
        keep = [str(k) for k in keep]
        placeholders = ",".join("?" for _ in keep) or "''"
        self.conn.execute(
            f"DELETE FROM identity_links WHERE channel = ? AND identity_id = ? AND external_id NOT IN ({placeholders})",
            (channel, identity_id, *keep),
        )

    def lookup_identity(self, channel: str, external_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT identity_id FROM identity_links WHERE channel = ? AND external_id = ?",
            (channel, str(external_id)),
        ).fetchone()
        return row["identity_id"] if row else None

    def identity_links(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM identity_links ORDER BY channel, external_id")]

    # ---------- conversations & messages ----------
    def ensure_conversation(self, conversation_id: str, channel: str, scope: str, title: str = "") -> None:
        existing = self.conn.execute("SELECT scope FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO conversations(id, channel, scope, title, created_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, channel, scope, title, self.clock()),
            )
            return
        if existing["scope"] != scope:
            raise StateConflict("conversation identity changed; use MemoryService.resolve for a source-aware migration")
        if title:
            self.conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))

    def list_conversations(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c ORDER BY COALESCE(c.last_message_at, c.created_at) DESC"
        )
        return [dict(r) for r in rows]

    def add_message(
        self,
        conversation_id: str,
        scope: str,
        role: str,
        content: str,
        ts: float,
        day_key: str,
        actor_id: str = "",
        actor_name: str = "",
        origin: str = "live",
        episode_id: Optional[str] = None,
        source_ref: Optional[str] = None,
    ) -> int:
        if source_ref:
            row = self.conn.execute("SELECT id FROM messages WHERE source_ref = ?", (source_ref,)).fetchone()
            if row:
                return int(row["id"])
        cursor = self.conn.execute(
            "INSERT INTO messages(conversation_id, scope, role, actor_id, actor_name, content, ts, day_key, origin, "
            "episode_id, source_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, scope, role, actor_id, actor_name, content, ts, day_key, origin, episode_id, source_ref),
        )
        self.conn.execute(
            "UPDATE conversations SET last_message_at = MAX(COALESCE(last_message_at, 0), ?) WHERE id = ?",
            (ts, conversation_id),
        )
        return int(cursor.lastrowid)

    def recent_messages(self, conversation_id: str, limit: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND LOWER(origin) != 'archived' "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_scope_messages(
        self, scope: str, exclude_conversation: str, since_ts: float, limit: int
    ) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE scope = ? AND conversation_id != ? AND ts >= ? "
            "AND LOWER(origin) != 'archived' ORDER BY ts DESC, id DESC LIMIT ?",
            (scope, exclude_conversation, since_ts, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def messages_page(self, conversation_id: str, offset: int, limit: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (conversation_id, limit, offset),
        )
        return [dict(r) for r in rows]

    def last_message(self, conversation_id: str, role: Optional[str] = None) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE conversation_id = ? AND LOWER(origin) != 'archived'"
        params: List[Any] = [conversation_id]
        if role:
            sql += " AND role = ?"
            params.append(role)
        row = self.conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT 1", params).fetchone()
        return dict(row) if row else None

    def pending_message_count(self, conversation_id: str) -> int:
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND {_PENDING_MESSAGES}",
            (conversation_id,),
        ).fetchone()
        return int(row["n"])

    def oldest_pending_messages(self, conversation_id: str, limit: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE conversation_id = ? AND {_PENDING_MESSAGES} ORDER BY ts, id LIMIT ?",
            (conversation_id, limit),
        )
        chunk = [dict(r) for r in rows]
        return [m for m in chunk if m["day_key"] == chunk[0]["day_key"]] if chunk else []

    def idle_pending_conversations(
        self, before_ts: float, minimum: int, before_day: str = "",
    ) -> List[Dict[str, Any]]:
        """Conversations with a small but idle tail; used to flush summaries without waiting for 40 messages."""
        rows = self.conn.execute(
            "SELECT conversation_id, scope, day_key, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
            f"FROM messages WHERE {_PENDING_MESSAGES} GROUP BY conversation_id, scope, day_key "
            "HAVING (COUNT(*) >= ? AND MAX(ts) <= ?) OR day_key < ? ORDER BY MIN(ts), conversation_id",
            (int(minimum), float(before_ts), before_day),
        )
        return [dict(r) for r in rows]

    def has_pending_messages_for_day(self, scope: str, day: str) -> bool:
        return self.conn.execute(
            f"SELECT 1 FROM messages WHERE scope = ? AND day_key = ? AND {_PENDING_MESSAGES} LIMIT 1",
            (scope, day),
        ).fetchone() is not None

    def messages_in_range(self, conversation_id: str, from_id: int, to_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND id BETWEEN ? AND ? ORDER BY id",
            (conversation_id, from_id, to_id),
        )
        return [dict(r) for r in rows]

    def mark_consolidated(self, conversation_id: str, from_id: int, to_id: int, episode_id: str) -> int:
        cursor = self.conn.execute(
            "UPDATE messages SET episode_id = ? WHERE conversation_id = ? AND id BETWEEN ? AND ? AND episode_id IS NULL",
            (episode_id, conversation_id, from_id, to_id),
        )
        return cursor.rowcount

    def pending_messages_in_range(self, conversation_id: str, from_id: int, to_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE conversation_id = ? AND id BETWEEN ? AND ? AND {_PENDING_MESSAGES} "
            "ORDER BY ts, id",
            (conversation_id, from_id, to_id),
        )
        return [dict(r) for r in rows]

    def mark_consolidated_ids(self, conversation_id: str, message_ids: Sequence[int], episode_id: str) -> int:
        changed = 0
        for offset in range(0, len(message_ids), 400):
            chunk = message_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            cursor = self.conn.execute(
                f"UPDATE messages SET episode_id = ? WHERE conversation_id = ? AND id IN ({placeholders}) "
                f"AND {_PENDING_MESSAGES}",
                (episode_id, conversation_id, *chunk),
            )
            changed += cursor.rowcount
        return changed

    # ---------- memories ----------
    def add_memory(
        self,
        kind: str,
        scope: str,
        content: str,
        *,
        conversation_id: Optional[str] = None,
        subject_id: Optional[str] = None,
        importance: float = 5.0,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        day_key: Optional[str] = None,
        tags: Sequence[str] = (),
        meta: Optional[Dict[str, Any]] = None,
        origin: str = "live",
        source_ref: Optional[str] = None,
        created_at: Optional[float] = None,
        memory_id: Optional[str] = None,
    ) -> str:
        if source_ref:
            row = self.conn.execute("SELECT id FROM memories WHERE source_ref = ?", (source_ref,)).fetchone()
            if row:
                return row["id"]
        memory_id = memory_id or uuid.uuid4().hex
        now = self.clock()
        self.conn.execute(
            f"INSERT INTO memories({_MEMORY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
            (
                memory_id, kind, scope, conversation_id, subject_id, content, float(importance), start_ts, end_ts,
                day_key, json.dumps(list(tags), ensure_ascii=False), json.dumps(meta or {}, ensure_ascii=False),
                origin, content_hash(content), source_ref, created_at or now, now,
            ),
        )
        return memory_id

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _memory_row(row) if row else None

    def get_memories(self, memory_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        rows = self.conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE id IN ({placeholders}) AND deleted = 0", list(memory_ids)
        )
        return {row["id"]: _memory_row(row) for row in rows}

    def find_memory_by_hash(self, scope: str, kind: str, digest: str, subject_id: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT id FROM memories WHERE scope = ? AND kind = ? AND content_hash = ? AND deleted = 0 "
            "AND COALESCE(subject_id, '') = COALESCE(?, '') LIMIT 1",
            (scope, kind, digest, subject_id),
        ).fetchone()
        return row["id"] if row else None

    def update_memory(self, memory_id: str, **fields: Any) -> bool:
        allowed = {"content", "importance", "tags", "meta", "subject_id", "day_key"}
        updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
        if not updates:
            return False
        if "tags" in updates:
            updates["tags"] = json.dumps(list(updates["tags"]), ensure_ascii=False)
        if "meta" in updates:
            updates["meta"] = json.dumps(updates["meta"], ensure_ascii=False)
        if "content" in updates:
            updates["content_hash"] = content_hash(updates["content"])
            updates["indexed_at"] = None
        updates["updated_at"] = self.clock()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        cursor = self.conn.execute(
            f"UPDATE memories SET {assignments} WHERE id = ? AND deleted = 0", (*updates.values(), memory_id)
        )
        return cursor.rowcount > 0

    def touch_memory(self, memory_id: str) -> None:
        self.conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (self.clock(), memory_id))

    def delete_memory(self, memory_id: str) -> bool:
        cursor = self.conn.execute(
            "UPDATE memories SET deleted = 1, updated_at = ? WHERE id = ? AND deleted = 0", (self.clock(), memory_id)
        )
        return cursor.rowcount > 0

    def list_memories(
        self,
        *,
        kinds: Optional[Sequence[str]] = None,
        scopes: Optional[Sequence[str]] = None,
        subject_id: Optional[str] = None,
        keyword: str = "",
        day_key: str = "",
        offset: int = 0,
        limit: int = 50,
        order: str = "recent",
    ) -> List[Dict[str, Any]]:
        sql, params = self._memory_filter(kinds, scopes, subject_id, keyword, day_key)
        if order == "profile":
            order_sql = "CASE WHEN origin = 'legacy' THEN 0 ELSE 1 END, importance DESC, updated_at DESC"
        else:
            order_sql = "importance DESC, updated_at DESC" if order == "importance" else "COALESCE(end_ts, created_at) DESC"
        rows = self.conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE {sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [_memory_row(r) for r in rows]

    def count_memories(self, **filters: Any) -> int:
        sql, params = self._memory_filter(
            filters.get("kinds"), filters.get("scopes"), filters.get("subject_id"),
            filters.get("keyword", ""), filters.get("day_key", ""),
        )
        return int(self.conn.execute(f"SELECT COUNT(*) AS n FROM memories WHERE {sql}", params).fetchone()["n"])

    @staticmethod
    def _memory_filter(kinds, scopes, subject_id, keyword, day_key):
        clauses = ["deleted = 0"]
        params: List[Any] = []
        if kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if scopes:
            clauses.append(f"scope IN ({','.join('?' for _ in scopes)})")
            params.extend(scopes)
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if keyword:
            clauses.append("content LIKE ?")
            params.append(f"%{keyword}%")
        if day_key:
            clauses.append("day_key = ?")
            params.append(day_key)
        return " AND ".join(clauses), params

    def latest_episode_before(self, conversation_id: str, before_ts: Optional[float]) -> Optional[Dict[str, Any]]:
        sql = f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE conversation_id = ? AND kind = 'episode' AND deleted = 0"
        params: List[Any] = [conversation_id]
        if before_ts is not None:
            sql += " AND COALESCE(end_ts, created_at) < ?"
            params.append(before_ts)
        row = self.conn.execute(sql + " ORDER BY COALESCE(end_ts, created_at) DESC LIMIT 1", params).fetchone()
        return _memory_row(row) if row else None

    def episodes_for_day(self, scope: str, day: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE scope = ? AND kind = 'episode' AND day_key = ? AND deleted = 0 "
            "ORDER BY COALESCE(start_ts, created_at), id",
            (scope, day),
        )
        return [_memory_row(r) for r in rows]

    def episode_days_before(self, scope: str, before_day: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT day_key FROM memories WHERE scope = ? AND kind = 'episode' "
            "AND deleted = 0 AND day_key < ? ORDER BY day_key",
            (scope, before_day),
        )
        return [r["day_key"] for r in rows]

    def undiaried_live_days(self, scope: str, before_day: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT e.day_key FROM memories e WHERE e.scope = ? AND e.kind = 'episode' "
            "AND e.deleted = 0 AND e.day_key < ? AND NOT EXISTS (SELECT 1 FROM memories d WHERE d.scope = e.scope "
            "AND d.kind = 'diary' AND d.day_key = e.day_key AND d.deleted = 0) ORDER BY e.day_key",
            (scope, before_day),
        )
        return [r["day_key"] for r in rows]

    def unindexed_memories(self, limit: int = 256) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE deleted = 0 AND indexed_at IS NULL LIMIT ?", (limit,)
        )
        return [_memory_row(r) for r in rows]

    def deleted_indexed_memory_ids(self, limit: Optional[int] = None) -> List[str]:
        rows = self.conn.execute("SELECT id FROM memories WHERE deleted = 1 AND indexed_at IS NOT NULL ORDER BY id"
                                 + (" LIMIT ?" if limit is not None else ""), (limit,) if limit is not None else ())
        return [r["id"] for r in rows]

    def mark_indexed(self, memory_ids: Sequence[str], indexed: bool = True) -> None:
        if not memory_ids:
            return
        placeholders = ",".join("?" for _ in memory_ids)
        self.conn.execute(
            f"UPDATE memories SET indexed_at = ? WHERE id IN ({placeholders})",
            (self.clock() if indexed else None, *memory_ids),
        )

    def clear_index_marks(self) -> None:
        self.conn.execute("UPDATE memories SET indexed_at = NULL")

    def all_active_memories(self, batch: int = 500) -> Iterator[List[Dict[str, Any]]]:
        offset = 0
        while True:
            rows = self.conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE deleted = 0 ORDER BY id LIMIT ? OFFSET ?",
                (batch, offset),
            ).fetchall()
            if not rows:
                return
            yield [_memory_row(r) for r in rows]
            offset += batch

    # ---------- state ----------
    def get_state(self, scope: str) -> Dict[str, Any]:
        row = self.conn.execute("SELECT data, version, updated_at FROM state WHERE scope = ?", (scope,)).fetchone()
        if not row:
            return {"data": {}, "version": 0, "updated_at": None}
        return {"data": json.loads(row["data"]), "version": int(row["version"]), "updated_at": row["updated_at"]}

    def put_state(self, scope: str, data: Dict[str, Any], expected_version: Optional[int] = None,
                  source_ref: Optional[str] = None, effective_at: Optional[float] = None) -> int:
        with self.transaction():
            current = self.get_state(scope)
            if expected_version is not None and current["version"] != expected_version:
                raise StateConflict(f"state version is {current['version']}, expected {expected_version}")
            version = current["version"] + 1
            self.conn.execute(
                "INSERT INTO state(scope, data, version, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET data = excluded.data, version = excluded.version, "
                "updated_at = excluded.updated_at",
                (scope, json.dumps(data, ensure_ascii=False), version, self.clock()),
            )
            # 时态历史是可选派生层；只有 MemoryService 挂载 TemporalStore 后才记录。
            temporal = getattr(self, "temporal", None)
            if temporal is not None:
                temporal.snapshot_state(scope, version, data, self.clock(),
                                        effective_at=effective_at, source_ref=source_ref)
        return version

    # ---------- jobs ----------
    def enqueue_job(
        self,
        kind: str,
        scope: str,
        payload: Dict[str, Any],
        dedupe_key: str,
        conversation_id: Optional[str] = None,
    ) -> Optional[int]:
        now = self.clock()
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO jobs(kind, scope, conversation_id, payload, status, available_at, dedupe_key, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (kind, scope, conversation_id, json.dumps(payload, ensure_ascii=False), now, dedupe_key, now, now),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def has_open_job(self, kind: str, conversation_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE kind = ? AND conversation_id = ? AND status IN ('pending', 'running') LIMIT 1",
            (kind, conversation_id),
        ).fetchone()
        return row is not None

    def has_open_diary_job(self, scope: str, day: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'diary' AND scope = ? AND status IN ('pending', 'running') "
            "AND json_extract(payload, '$.day_key') = ? LIMIT 1",
            (scope, day),
        ).fetchone() is not None

    def update_job_payload(self, job_id: int, payload: Dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE jobs SET payload = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (json.dumps(payload, ensure_ascii=False), self.clock(), job_id),
        )

    def claim_job(self, lease_seconds: int, kinds: Optional[Sequence[str]] = None,
                  excluded: Sequence[int] = (), max_lease_recoveries: Optional[int] = None) -> Optional[Dict[str, Any]]:
        now = self.clock()
        sql = (
            "SELECT * FROM jobs WHERE ((status = 'pending' AND available_at <= ?) "
            "OR (status = 'running' AND lease_until < ?))"
        )
        params: List[Any] = [now, now]
        if kinds:
            sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        if excluded:
            sql += f" AND id NOT IN ({','.join('?' for _ in excluded)})"
            params.extend(excluded)
        with self.transaction():
            if max_lease_recoveries is not None:
                expired = self.conn.execute("SELECT id,payload FROM jobs WHERE status='running' AND lease_until<? "
                                            "AND lease_recoveries>=?", (now, max_lease_recoveries)).fetchall()
                for row in expired:
                    payload = {**json.loads(row["payload"]), "terminal_reason": "lease_recovery_budget"}
                    self.conn.execute("UPDATE jobs SET status='failed',lease_until=NULL,updated_at=?,payload=?, "
                                      "last_error='lease recovery budget exhausted' WHERE id=?",
                                      (now, json.dumps(payload), row["id"]))
            row = self.conn.execute(sql + " ORDER BY CASE WHEN kind='repair_memory' THEN 0 "
                                    "WHEN kind IN ('update_state','audit_memory') THEN 1 "
                                    "WHEN kind='associate' AND COALESCE(json_extract(payload,'$.background'),0)=0 THEN 2 "
                                    "WHEN kind='associate' THEN 4 ELSE 3 END, available_at, id LIMIT 1",
                                    params).fetchone()
            if not row:
                return None
            self.conn.execute(
                "UPDATE jobs SET lease_recoveries=lease_recoveries+CASE WHEN status='running' THEN 1 ELSE 0 END, "
                "status = 'running', attempts = attempts + 1, lease_until = ?, "
                "started_at = ?, duration_seconds = NULL, updated_at = ? WHERE id = ?",
                (now + max(lease_seconds, 420 if row["kind"] in {"associate", "audit_memory"} else lease_seconds), now, now, row["id"]),
            )
        job = dict(self.conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
        job["payload"] = json.loads(job["payload"])
        return job

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        job = dict(row)
        job["payload"] = json.loads(job["payload"])
        return job

    def finish_job(self, job_id: int, *, release_dedupe: bool = False) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = 'done', lease_until = NULL, last_error = NULL, "
            "duration_seconds = CASE WHEN started_at IS NOT NULL THEN MAX(0, ? - started_at) END, "
            "dedupe_key = CASE WHEN ? THEN NULL ELSE dedupe_key END, updated_at = ? WHERE id = ?",
            (self.clock(), release_dedupe, self.clock(), job_id),
        )

    def fail_job(self, job_id: int, error: str, max_attempts: int) -> str:
        job = self.get_job(job_id)
        if not job:
            return "missing"
        if job["status"] != "running":
            return job["status"]  # 撤回或手动更新取消的任务不能因迟到的失败通知复活。
        now = self.clock()
        failures = int(job["payload"].get("generation_failures", 0)) + 1
        self.update_job_payload(job_id, {**job["payload"], "generation_failures": failures})
        if failures >= max_attempts:
            status, available_at = "failed", now
        else:
            status, available_at = "pending", now + 60 * (2 ** min(10, failures - 1))
        self.conn.execute(
            "UPDATE jobs SET status = ?, available_at = ?, lease_until = NULL, last_error = ?, "
            "duration_seconds = CASE WHEN started_at IS NOT NULL THEN MAX(0, ? - started_at) END, updated_at = ? WHERE id = ?",
            (status, available_at, error[:2000], now, now, job_id),
        )
        return status

    def job_counts(self) -> Dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    # ---------- migration ledger ----------
    def ledger_get(self, source: str, source_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM migration_ledger WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
        return dict(row) if row else None

    def ledger_add(self, source: str, source_id: str, target_table: str, target_id: str, digest: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO migration_ledger(source, source_id, target_table, target_id, content_hash, migrated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, source_id, target_table, str(target_id), digest, self.clock()),
        )

    def ledger_entries(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM migration_ledger ORDER BY source, source_id")]

    def get_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    # ---------- stats ----------
    def stats(self) -> Dict[str, Any]:
        memory_rows = self.conn.execute(
            "SELECT scope, kind, COUNT(*) AS n FROM memories WHERE deleted = 0 GROUP BY scope, kind"
        )
        memories: Dict[str, Dict[str, int]] = {}
        for row in memory_rows:
            memories.setdefault(row["scope"], {})[row["kind"]] = int(row["n"])
        message_rows = self.conn.execute("SELECT scope, COUNT(*) AS n FROM messages GROUP BY scope")
        unindexed = self.conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE deleted = 0 AND indexed_at IS NULL"
        ).fetchone()["n"]
        return {
            "memories": memories,
            "messages": {r["scope"]: int(r["n"]) for r in message_rows},
            "jobs": self.job_counts(),
            "unindexed": int(unindexed),
        }
