"""Rebuildable retrieval keys; messages and memories remain the source of truth.

No models, network access, repository migrations, or source-table writes. Call
sync explicitly before retrieval. Vector consumers should serialize
sync -> unindexed_keys -> upsert -> mark_indexed, and reconcile deletions against
all_ids(). Only retrieval_keys.indexed_at belongs to these consumers.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .models import ORIGIN_LEGACY, ROLES, is_system_event
from .repository import MemoryRepository

CHUNK_SIZE = 700
CHUNK_OVERLAP = 80
_BATCH_SIZE = 400
_TOKEN_RUNS = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]+"
    r"|[a-zA-Z0-9]+(?:[_'-][a-zA-Z0-9]+)*"
)
_RECALLED = {"[\u6d88\u606f\u5df2\u64a4\u56de]", "[message recalled]", "[message deleted]"}
_INACTIVE = {"recalled", "withdrawn", "retracted", "deleted", "system"}
_EXPANSIONS = ("retrieval", "context", "entities", "keywords", "aliases")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS retrieval_keys (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        kind TEXT NOT NULL,
        source_type TEXT NOT NULL CHECK (source_type IN ('memory', 'message')),
        memory_id TEXT,
        message_id INTEGER,
        source_ref TEXT,
        chunk_index INTEGER NOT NULL,
        content TEXT NOT NULL,
        search_text TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        indexed_at REAL,
        meta TEXT NOT NULL,
        tokens TEXT NOT NULL,
        CHECK ((source_type = 'memory' AND memory_id IS NOT NULL AND message_id IS NULL)
            OR (source_type = 'message' AND message_id IS NOT NULL AND memory_id IS NULL))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_keys_scope ON retrieval_keys(scope, kind)",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_keys_pending ON retrieval_keys(indexed_at)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_keys_fts
        USING fts5(id UNINDEXED, tokens, tokenize='unicode61 remove_diacritics 2')""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_keys_ai AFTER INSERT ON retrieval_keys BEGIN
        INSERT INTO retrieval_keys_fts(rowid, id, tokens) VALUES (new.rowid, new.id, new.tokens);
    END""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_keys_ad AFTER DELETE ON retrieval_keys BEGIN
        DELETE FROM retrieval_keys_fts WHERE rowid = old.rowid;
    END""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_keys_au
        AFTER UPDATE OF id, tokens ON retrieval_keys BEGIN
        DELETE FROM retrieval_keys_fts WHERE rowid = old.rowid;
        INSERT INTO retrieval_keys_fts(rowid, id, tokens) VALUES (new.rowid, new.id, new.tokens);
    END""",
)

_SOURCE_JOIN = """
    LEFT JOIN memories m ON k.source_type = 'memory' AND m.id = k.memory_id
    LEFT JOIN messages u ON k.source_type = 'message' AND u.id = k.message_id
"""
_SOURCE_EXISTS = """(
    (k.source_type = 'memory' AND m.id IS NOT NULL AND m.deleted = 0 AND m.scope = k.scope)
    OR (k.source_type = 'message' AND u.id IS NOT NULL AND u.scope = k.scope)
)"""


def _object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tokens(text: str) -> List[str]:
    tokens = []
    for match in _TOKEN_RUNS.finditer(text or ""):
        word = match.group().lower()
        if word[0].isascii():
            tokens.append(word)
        else:
            tokens.extend(word)
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
    return tokens


def _chunks(text: str) -> Iterator[str]:
    for paragraph in re.split(r"\r?\n[ \t]*\r?\n", text):
        start = 0
        while start < len(paragraph):
            end = min(start + CHUNK_SIZE, len(paragraph))
            chunk = paragraph[start:end]
            if chunk.strip():
                yield chunk
            if end == len(paragraph):
                break
            start = end - CHUNK_OVERLAP


def _short_evidence(text: str, query: str, limit: int = 480) -> str:
    """Use one contiguous source span rather than replaying a whole transcript."""
    if len(text) <= limit:
        return text
    terms = set(_tokens(query))
    pieces = list(_chunks(text))
    selected = max(pieces, key=lambda part: len(terms.intersection(_tokens(part)))) if pieces else text
    return selected[:limit]


def _expansions(meta: Mapping[str, Any]) -> List[Tuple[str, str]]:
    result = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, str) and value.strip():
            result.append((path, value))
        elif isinstance(value, list):
            for item in value:
                visit(item, path)
        elif isinstance(value, dict):
            for name, item in sorted(value.items()):
                # Alias maps may hold the canonical entity in the dictionary key.
                if path.endswith(".aliases") and name.strip():
                    result.append((path, name))
                visit(item, f"{path}.{name}")

    for name in _EXPANSIONS:
        visit(meta.get(name), f"meta.{name}")
    return result


def _active(row: Mapping[str, Any], source_type: str, *, include_archived: bool = False) -> bool:
    text = row.get("content") or ""
    meta = _object(row.get("meta"))
    if not text.strip() or text.strip().lower() in _RECALLED or is_system_event(text):
        return False
    if source_type == "message" and row.get("role") not in ROLES:
        return False
    if source_type == "message" and row.get("origin") == "archived" and not include_archived:
        return False
    if source_type == "message" and row.get("episode_id") and not include_archived:
        return False  # Consolidated dialogue is reached through its summary evidence.
    if (source_type == "memory" and meta.get("legacy_type") == "raw_conversation"
            and not meta.get("curated_quote")):
        return False
    for data in (row, meta):
        if any(data.get(flag) in (True, 1, "true", "1")
               for flag in ("deleted", "recalled", "withdrawn", "retracted", "is_system_event")):
            return False
        if str(data.get("status", "")).lower() in _INACTIVE:
            return False
    return str(row.get("origin", "")).lower() not in _INACTIVE


def _fingerprint(row: Mapping[str, Any], source_type: str) -> str:
    # Hash exact text, not models.content_hash's whitespace-normalized text.
    # Exclude lifecycle timestamps and vector marks so no-op sync stays a no-op.
    names = (
        "id", "scope", "content", "kind", "conversation_id", "subject_id", "role",
        "actor_id", "actor_name", "origin", "source_ref", "ts", "start_ts", "end_ts",
        "day_key", "episode_id", "tags",
    )
    payload = {name: row.get(name) for name in names}
    payload.update(source_type=source_type, meta=_object(row.get("meta")),
                   key_version=1, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _batches(values: Sequence[Any]) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), _BATCH_SIZE):
        yield values[start:start + _BATCH_SIZE]


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _strings(values: Sequence[str]) -> List[str]:
    return list(dict.fromkeys([values] if isinstance(values, str) else values))


class EvidenceStore:
    """Derived storage on repo.conn; construction creates only its own schema."""

    def __init__(self, repo: MemoryRepository):
        self.repo = repo
        self.conn = repo.conn
        self.ensure_schema()

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        # Unlike executescript or a nested repo.transaction(), this never commits
        # the caller's transaction and rolls back partial DDL/index writes.
        self.conn.execute("SAVEPOINT evidence_store")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK TO evidence_store")
            raise
        finally:
            self.conn.execute("RELEASE evidence_store")

    def ensure_schema(self) -> None:
        """Fail atomically if FTS5 is unavailable; never change schema_version."""
        with self._atomic():
            for statement in _SCHEMA:
                self.conn.execute(statement)

    def _source_keys(self, row: Dict[str, Any], source_type: str) -> Iterator[Dict[str, Any]]:
        digest = _fingerprint(row, source_type)
        expansions = _expansions(_object(row.get("meta")))
        context_names = ("conversation_id", "subject_id", "actor_id", "actor_name", "role", "day_key")
        context = [str(row[name]) for name in context_names if row.get(name)]
        if row.get("tags"):
            try:
                tags = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
            except (TypeError, ValueError):
                tags = []
            if isinstance(tags, list):
                context.extend(tag for tag in tags if isinstance(tag, str))
        prefix = "m" if source_type == "memory" else "u"
        for chunk_index, chunk in enumerate(_chunks(row["content"])):
            yield {
                "id": f"{prefix}:{row['id']}:{chunk_index}",
                "scope": row["scope"],
                "kind": row["kind"] if source_type == "memory" else "message",
                "source_type": source_type,
                "memory_id": row["id"] if source_type == "memory" else None,
                "message_id": row["id"] if source_type == "message" else None,
                "source_ref": row.get("source_ref"),
                "chunk_index": chunk_index,
                "content": chunk,
                "search_text": "\n".join([chunk, *context, *(text for _, text in expansions)]),
                "source_hash": digest,
                "meta": _json({
                    "source_type": source_type, "source_id": row["id"],
                    "source_ref": row.get("source_ref"), "origin": row.get("origin"),
                    "expansion_sources": sorted({path for path, _ in expansions}),
                }),
            }

    def sync(self) -> Dict[str, int]:
        """Incrementally upsert/prune keys and repair FTS from current sources.

        Stats count keys, not source rows. Changed source text, scope, metadata or
        provenance invalidates its vector marks. No-op sync preserves them.
        """
        stats = dict(added=0, updated=0, deleted=0, unchanged=0, total=0, fts_repaired=0)
        with self._atomic():
            self.ensure_schema()
            existing = {row["id"]: dict(row) for row in self.conn.execute("SELECT * FROM retrieval_keys")}
            seen = set()
            for source_type, table in (("memory", "memories"), ("message", "messages")):
                for raw in self.conn.execute(f"SELECT * FROM {table} ORDER BY id"):
                    row = dict(raw)
                    if not _active(row, source_type):
                        continue
                    for key in self._source_keys(row, source_type):
                        key_id = key["id"]
                        seen.add(key_id)
                        old = existing.get(key_id)
                        if old and all(old[name] == value for name, value in key.items()):
                            stats["unchanged"] += 1
                            continue
                        key["tokens"] = " ".join(_tokens(key["search_text"]))
                        columns = list(key)
                        updates = ", ".join(f"{name} = excluded.{name}" for name in columns if name != "id")
                        self.conn.execute(
                            f"INSERT INTO retrieval_keys({', '.join(columns)}) "
                            f"VALUES ({_placeholders(columns)}) ON CONFLICT(id) DO UPDATE SET "
                            f"{updates}, indexed_at = NULL",
                            tuple(key.values()),
                        )
                        stats["updated" if old else "added"] += 1
            obsolete = set(existing) - seen
            self.conn.executemany("DELETE FROM retrieval_keys WHERE id = ?", ((key_id,) for key_id in obsolete))
            stats["deleted"] = len(obsolete)
            # Repair a missing, stale or partially rebuilt FTS without touching
            # healthy rows or the independently managed vector marks.
            removed = self.conn.execute(
                "DELETE FROM retrieval_keys_fts WHERE rowid IN ("
                "SELECT f.rowid FROM retrieval_keys_fts f LEFT JOIN retrieval_keys k ON k.rowid = f.rowid "
                "WHERE k.id IS NULL OR f.id IS NOT k.id OR f.tokens IS NOT k.tokens)"
            ).rowcount
            inserted = self.conn.execute(
                "INSERT INTO retrieval_keys_fts(rowid, id, tokens) "
                "SELECT k.rowid, k.id, k.tokens FROM retrieval_keys k WHERE NOT EXISTS "
                "(SELECT 1 FROM retrieval_keys_fts f WHERE f.rowid = k.rowid)"
            ).rowcount
            stats.update(total=len(seen), fts_repaired=removed + inserted)
        return stats

    def _live_keys(self, rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Revalidate hashes after SQL authorization, including changes before sync."""
        sources = {}
        for source_type, table, field in (
            ("memory", "memories", "memory_id"), ("message", "messages", "message_id"),
        ):
            selected = [row for row in rows if row["source_type"] == source_type]
            ids = list({row[field] for row in selected})
            # Fetch only source scopes already admitted by the caller's SQL.
            scopes = list({row["scope"] for row in selected})
            for batch in _batches(ids):
                for raw in self.conn.execute(
                    f"SELECT * FROM {table} WHERE id IN ({_placeholders(batch)}) "
                    f"AND scope IN ({_placeholders(scopes)})", (*batch, *scopes),
                ):
                    source = dict(raw)
                    if _active(source, source_type):
                        sources[source_type, source["id"]] = (source["scope"], _fingerprint(source, source_type))
        result = {}
        for row in rows:
            source_id = row["memory_id"] if row["source_type"] == "memory" else row["message_id"]
            if sources.get((row["source_type"], source_id)) != (row["scope"], row["source_hash"]):
                continue
            item = dict(row)
            item["meta"] = _object(item["meta"])
            item.pop("tokens", None)
            result[item["id"]] = item
        return result

    def get_keys(self, ids: Sequence[str], scopes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Return only authorized keys whose source is still active and unchanged."""
        ids, scopes = _strings(ids), _strings(scopes)
        if not ids or not scopes:
            return {}
        result = {}
        with self._atomic():
            for batch in _batches(ids):
                rows = self.conn.execute(
                    f"SELECT k.* FROM retrieval_keys k {_SOURCE_JOIN} "
                    f"WHERE k.id IN ({_placeholders(batch)}) AND k.scope IN ({_placeholders(scopes)}) "
                    f"AND {_SOURCE_EXISTS}", (*batch, *scopes),
                ).fetchall()
                result.update(self._live_keys(rows))
        return result

    def bm25_query(
        self, query: str, scopes: Sequence[str], kinds: Optional[Sequence[str]] = None, limit: int = 40,
    ) -> List[Tuple[str, float]]:
        """Return (key_id, raw FTS5 bm25), ascending: smaller is more relevant."""
        scopes = _strings(scopes)
        terms = list(dict.fromkeys(_tokens(query)))[:256]
        if not scopes or not terms or limit <= 0:
            return []
        conditions = [f"k.scope IN ({_placeholders(scopes)})", _SOURCE_EXISTS]
        params: List[Any] = list(scopes)
        if kinds is not None:
            kinds = _strings(kinds)
            if not kinds:
                return []
            conditions.append(f"k.kind IN ({_placeholders(kinds)})")
            params.extend(kinds)
        conditions.append("retrieval_keys_fts MATCH ?")
        # Generated tokens are always quoted data, never caller-supplied FTS syntax.
        params.append(" OR ".join('"' + term.replace('"', '""') + '"' for term in terms))
        hits = []
        with self._atomic():
            cursor = self.conn.execute(
                "SELECT k.*, bm25(retrieval_keys_fts) AS score FROM retrieval_keys_fts "
                "JOIN retrieval_keys k ON k.rowid = retrieval_keys_fts.rowid AND k.id = retrieval_keys_fts.id "
                f"{_SOURCE_JOIN} WHERE {' AND '.join(conditions)} ORDER BY score, k.id", params,
            )
            # Apply the limit after freshness checks so stale hits cannot crowd
            # out valid ones. Scope/kind filtering is already done inside SQL.
            while len(hits) < limit:
                rows = cursor.fetchmany(_BATCH_SIZE)
                if not rows:
                    break
                live = self._live_keys(rows)
                hits.extend((row["id"], float(row["score"])) for row in rows if row["id"] in live)
        return hits[:limit]

    def _current_keys(self, only_unindexed: bool = False, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        result = []
        with self._atomic():
            cursor = self.conn.execute(
                "SELECT * FROM retrieval_keys " + ("WHERE indexed_at IS NULL " if only_unindexed else "") +
                "ORDER BY id"
            )
            while limit is None or len(result) < limit:
                rows = cursor.fetchmany(_BATCH_SIZE)
                if not rows:
                    break
                result.extend(self._live_keys(rows).values())
        return result if limit is None else result[:limit]

    def unindexed_keys(self, limit: int = 128) -> List[Dict[str, Any]]:
        return self._current_keys(only_unindexed=True, limit=limit)

    def mark_indexed(self, ids: Sequence[str]) -> None:
        ids = _strings(ids)
        with self._atomic():
            for batch in _batches(ids):
                rows = self.conn.execute(
                    f"SELECT * FROM retrieval_keys WHERE id IN ({_placeholders(batch)})", batch,
                ).fetchall()
                self.conn.executemany(
                    "UPDATE retrieval_keys SET indexed_at = ? WHERE id = ?",
                    ((self.repo.clock(), key_id) for key_id in self._live_keys(rows)),
                )

    def all_ids(self) -> List[str]:
        """Current live key IDs, including indexed keys; for vector deletion reconciliation."""
        return [row["id"] for row in self._current_keys()]

    def clear_index_marks(self) -> None:
        with self._atomic():
            self.conn.execute("UPDATE retrieval_keys SET indexed_at = NULL")

    @staticmethod
    def _message_range(value: Any) -> Optional[Tuple[int, int]]:
        if isinstance(value, dict):
            value = [value.get("from_id", value.get("start")), value.get("to_id", value.get("end"))]
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            return None
        if any(isinstance(v, bool) or not re.fullmatch(r"[0-9]+", str(v)) for v in value):
            return None
        start, end = map(int, value)
        return (start, end) if 0 < start <= end <= 2**63 - 1 else None

    def _memory_messages(self, memory: Dict[str, Any]) -> List[Dict[str, Any]]:
        scope = memory["scope"]
        pending, seen, messages = [memory], set(), {}
        while pending:
            current = pending.pop()
            if current["id"] in seen:
                continue
            seen.add(current["id"])
            meta = _object(current.get("meta"))
            episode_id = meta.get("episode_id")
            clauses, params = [], [scope]
            explicit_ids = [
                int(value) for value in meta.get("source_message_ids") or []
                if not isinstance(value, bool) and re.fullmatch(r"\d+", str(value))
            ]
            if explicit_ids:
                clauses.append(f"id IN ({_placeholders(explicit_ids)})")
                params.extend(explicit_ids)
            conversation_id = current.get("conversation_id") or memory.get("conversation_id")
            span = self._message_range(meta.get("message_range"))
            if span and conversation_id:
                clauses.append("(conversation_id = ? AND id BETWEEN ? AND ?)")
                params.extend((conversation_id, *span))
            episode_ids = [current["id"]] if current["kind"] == "episode" else []
            if isinstance(episode_id, str) and episode_id:
                linked = self.conn.execute(
                    "SELECT * FROM memories WHERE id = ? AND scope = ? AND deleted = 0", (episode_id, scope),
                ).fetchone()
                if linked and _active(dict(linked), "memory"):
                    pending.append(dict(linked))
                episode_ids.append(episode_id)
            for ref in episode_ids:
                clause = "episode_id = ?"
                params.append(ref)
                if conversation_id:
                    clause += " AND conversation_id = ?"
                    params.append(conversation_id)
                clauses.append(f"({clause})")
            if not clauses:
                continue
            for raw in self.conn.execute(
                f"SELECT * FROM messages WHERE scope = ? AND ({' OR '.join(clauses)})", params,
            ):
                message = dict(raw)
                if _active(message, "message", include_archived=True):
                    messages[message["id"]] = message
        return list(messages.values())

    def evidence(
        self, key: str | Mapping[str, Any], scopes: Sequence[str], query: str = "", limit: int = 4,
    ) -> List[Dict[str, Any]]:
        """Resolve real events, or explicitly label a verbatim summary fallback.

        A supplied key row is untrusted: only its ID is used. References must stay
        in the key's own scope, even if the caller can access several scopes.
        """
        if limit <= 0:
            return []
        key_id = key if isinstance(key, str) else key.get("id")
        if not key_id:
            return []
        with self._atomic():
            current = self.get_keys([key_id], scopes).get(key_id)
            if not current:
                return []
            source_type = current["source_type"]
            table, field = ("messages", "message_id") if source_type == "message" else ("memories", "memory_id")
            source = dict(self.conn.execute(
                f"SELECT * FROM {table} WHERE id = ? AND scope = ?", (current[field], current["scope"]),
            ).fetchone())
            messages = [source] if source_type == "message" else self._memory_messages(source)
            if messages:
                terms = set(_tokens(query))
                messages.sort(key=lambda row: (
                    -len(terms.intersection(_tokens(row["content"]))), row["ts"], row["id"],
                ))
                return [{
                    "source_type": "message", "source_id": row["id"],
                    "source_ref": row.get("source_ref") or f"messages:{row['id']}",
                    "role": row["role"], "actor_id": row["actor_id"], "content": _short_evidence(row["content"], query),
                    "ts": row["ts"], "scope": row["scope"],
                } for row in messages[:limit]]
            return [{
                "source_type": "legacy_memory" if source.get("origin") == ORIGIN_LEGACY else "summary",
                "source_id": source["id"],
                "source_ref": source.get("source_ref") or f"memories:{source['id']}",
                "role": None, "actor_id": None, "content": _short_evidence(source["content"], query),
                "ts": source["end_ts"] if source.get("end_ts") is not None else source.get("created_at"),
                "scope": source["scope"],
            }]
