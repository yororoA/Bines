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
from .read_cache import read_cache

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
_FINGERPRINT_FIELDS = (
    "id", "scope", "content", "kind", "conversation_id", "subject_id", "role",
    "actor_id", "actor_name", "origin", "source_ref", "ts", "start_ts", "end_ts",
    "day_key", "episode_id", "tags",
)
_SOURCE_UPDATE_FIELDS = ",".join((*_FINGERPRINT_FIELDS, "meta", "deleted", "recalled", "withdrawn",
                                 "retracted", "is_system_event", "status"))

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
    "CREATE INDEX IF NOT EXISTS idx_retrieval_keys_memory ON retrieval_keys(memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_keys_message ON retrieval_keys(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_message_episode ON messages(episode_id,scope)",
    """CREATE TABLE IF NOT EXISTS retrieval_dirty_sources (
        source_type TEXT NOT NULL, source_id TEXT NOT NULL,
        PRIMARY KEY(source_type, source_id))""",
    """CREATE TABLE IF NOT EXISTS retrieval_dependencies (
        source_type TEXT NOT NULL, source_id TEXT NOT NULL,
        dependent_type TEXT NOT NULL, dependent_id TEXT NOT NULL,
        PRIMARY KEY(source_type,source_id,dependent_type,dependent_id))""",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_dependents ON retrieval_dependencies(dependent_type,dependent_id)",
    "CREATE TABLE IF NOT EXISTS retrieval_sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS retrieval_vector_deletions (id TEXT PRIMARY KEY)",
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
    matching = sorted((term for term in terms if len(term) > 1 and term in selected.lower()), key=len, reverse=True)
    position = selected.lower().find(matching[0]) if matching else 0
    start = max(0, min(position - limit // 3, len(selected) - limit))
    return selected[start:start + limit]


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
    if source_type == "memory" and (meta.get("audit_only") or meta.get("repair_pending")):
        return False
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
    payload = {name: row.get(name) for name in _FINGERPRINT_FIELDS}
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
        self.reads = read_cache(repo)
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
            tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for statement in _SCHEMA:
                self.conn.execute(statement)
            tracking_changed = self._install_dirty_triggers(tables)
            initialized = self.conn.execute(
                "SELECT 1 FROM retrieval_sync_state WHERE key='dirty_sources_v1'").fetchone()
            if tracking_changed or not initialized or "retrieval_keys" not in tables:
                self._queue_all_sources()
                self.conn.execute("INSERT OR REPLACE INTO retrieval_sync_state VALUES('dirty_sources_v1','1')")
            if "retrieval_keys_fts" not in tables:
                self.conn.execute("INSERT OR REPLACE INTO retrieval_sync_state VALUES('repair_fts','1')")

    def _install_dirty_triggers(self, tables):
        # SQLite triggers, rather than Python callbacks/total_changes, also catch
        # direct SQL and other connections, and roll back with the source write.
        triggers = {r[0]: r[1] for r in self.conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'retrieval_dirty_%'")}
        changed = False
        # SQLite permits absent names in UPDATE OF. Include importer extensions
        # before they exist, so ALTER TABLE followed by UPDATE cannot slip through
        # the interval before our next sync; never reference those optional values
        # directly in the trigger body.
        specs = [
            ("memories", "memory", "id", _SOURCE_UPDATE_FIELDS, ""),
            ("messages", "message", "id", _SOURCE_UPDATE_FIELDS,
             "INSERT OR IGNORE INTO retrieval_dirty_sources VALUES('conversation',{alias}.conversation_id);"
             "INSERT OR IGNORE INTO retrieval_dirty_sources SELECT 'message_ref',{alias}.source_ref WHERE {alias}.source_ref IS NOT NULL;"
             "INSERT OR IGNORE INTO retrieval_dirty_sources SELECT 'memory',{alias}.episode_id WHERE {alias}.episode_id IS NOT NULL;"),
        ]
        if "experiences" in tables:
            specs.append(("experiences", "experience", "source_ref", "source_ref,scope,record,memory_id", ""))
        if "association_nodes" in tables:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_retrieval_node_memory ON association_nodes(memory_id)")
            specs.append(("association_nodes", "node", "id",
                          "id,scope,memory_id,message_ids,family,digest,revision,retrieval_status,superseded",
                          "INSERT OR IGNORE INTO retrieval_dirty_sources SELECT 'memory',{alias}.memory_id WHERE {alias}.memory_id IS NOT NULL;"
                          "INSERT OR IGNORE INTO retrieval_dirty_sources SELECT 'message',CAST(value AS TEXT) FROM json_each({alias}.message_ids) WHERE true;"))
        if "memory_corrections" in tables:
            specs.append(("memory_corrections", "node", "node_id", "node_id,scope,kind,quote,replacement", ""))
        if "association_message_nodes" in tables:
            specs.append(("association_message_nodes", "message", "message_id", "message_id,node_id", ""))
        for table, kind, field, columns, extra in specs:
            for event, suffix, aliases in (("INSERT", "ai", ("new",)), ("UPDATE OF " + columns, "au", ("old", "new")),
                                           ("DELETE", "ad", ("old",))):
                body = "".join(
                    f"INSERT OR IGNORE INTO retrieval_dirty_sources VALUES('{kind}',CAST({alias}.{field} AS TEXT));"
                    + extra.format(alias=alias) for alias in aliases)
                # An outer UPSERT can override a trigger's INSERT OR IGNORE.
                # The explicit UPSERT clause retains idempotence in that case.
                body = body.replace("INSERT OR IGNORE INTO", "INSERT INTO").replace(
                    ";", " ON CONFLICT(source_type,source_id) DO NOTHING;")
                name = f"retrieval_dirty_{table}_{suffix}"
                statement = f"CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN {body} END"
                if triggers.get(name) != statement:
                    self.conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                    self.conn.execute(statement)
                    changed = True
        self.conn.execute("""CREATE TRIGGER IF NOT EXISTS retrieval_keys_dirty_ad AFTER DELETE ON retrieval_keys BEGIN
            INSERT INTO retrieval_vector_deletions VALUES(old.id) ON CONFLICT(id) DO NOTHING;
            INSERT INTO retrieval_dirty_sources VALUES(old.source_type,COALESCE(old.memory_id,CAST(old.message_id AS TEXT)))
                ON CONFLICT(source_type,source_id) DO NOTHING;
        END""")
        return changed

    def _queue_all_sources(self):
        for kind, table in (("memory", "memories"), ("message", "messages")):
            self.conn.execute(f"INSERT OR IGNORE INTO retrieval_dirty_sources SELECT ?,CAST(id AS TEXT) FROM {table}", (kind,))
        # Include missing sources so a rebuild also removes orphaned keys.
        self.conn.execute("""INSERT OR IGNORE INTO retrieval_dirty_sources
            SELECT source_type,COALESCE(memory_id,CAST(message_id AS TEXT)) FROM retrieval_keys""")

    def _source_fingerprint(self, row, source_type):
        fingerprint = _fingerprint(row, source_type)
        experience = self._experience_record(row) if source_type == "memory" else None
        return hashlib.sha256(_json([fingerprint, experience]).encode()).hexdigest() if experience is not None else fingerprint

    def _replace_dependencies(self, row, source_type):
        source_id = str(row["id"])
        self.conn.execute("DELETE FROM retrieval_dependencies WHERE dependent_type=? AND dependent_id=?", (source_type, source_id))
        dependencies = set()
        if source_type == "memory":
            meta = _object(row.get("meta"))
            dependencies.update(("message", str(mid)) for mid in meta.get("source_message_ids") or [])
            if meta.get("fact_source_message_id"):
                dependencies.add(("message", str(meta["fact_source_message_id"])))
            dependencies.update(("memory", str(mid)) for mid in [*(meta.get("source_memory_ids") or []), *(meta.get("episode_ids") or [])])
            if meta.get("episode_id"):
                dependencies.add(("memory", str(meta["episode_id"])))
            if meta.get("experience_ref"):
                dependencies.add(("experience", str(meta["experience_ref"])))
            dependencies.update(("experience", str(ref)) for ref in meta.get("source_experience_refs") or [])
            for ref in meta.get("source_message_refs") or []:
                dependencies.add(("message_ref", str(ref)))
                message = self.conn.execute("SELECT id FROM messages WHERE source_ref=? AND scope=?", (ref, row["scope"])).fetchone()
                if message:
                    dependencies.add(("message", str(message[0])))
            if row.get("kind") == "episode":
                dependencies.update(("message", str(r[0])) for r in self.conn.execute(
                    "SELECT id FROM messages WHERE episode_id=? AND scope=?", (row["id"], row["scope"])))
            if row.get("conversation_id") and meta.get("message_range"):
                dependencies.add(("conversation", row["conversation_id"]))
        graph = getattr(self.repo, "associations", None)
        node = (graph.for_memory(row["id"]) if source_type == "memory" else graph.for_message(row["id"])) if graph else None
        if node:
            dependencies.add(("node", node["id"]))
            for root in node["family"]:
                kind, _, sid = root.partition(":")
                if kind in {"memory", "message", "experience"}:
                    dependencies.add((kind, sid))
        self.conn.executemany("INSERT OR IGNORE INTO retrieval_dependencies VALUES(?,?,?,?)",
                             [(kind, sid, source_type, source_id) for kind, sid in dependencies
                              if (kind, sid) != (source_type, source_id)])

    def _source_keys(self, row: Dict[str, Any], source_type: str) -> Iterator[Dict[str, Any]]:
        digest = self._source_fingerprint(row, source_type)
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
        source_text = row["content"]
        experience = self._experience_record(row) if source_type == "memory" else None
        if experience:
            from .experience import text
            source_text += "\n" + text(experience.get("args", {})) + "\n" + text(experience.get("result", ""))
        for chunk_index, chunk in enumerate(_chunks(source_text)):
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

    def _direct_source(self, row, source_type):
        retention = getattr(self.repo, "retention", None)
        status = retention.direct(row, source_type) if retention else None
        if status is False:
            return False
        return _active(row, source_type, include_archived=status is True)

    def _experience_record(self, memory):
        ref = _object(memory.get("meta")).get("experience_ref")
        if not ref:
            return None
        row = self.conn.execute("SELECT record FROM experiences WHERE source_ref=? AND scope=?",
                                (ref, memory["scope"])).fetchone()
        return json.loads(row["record"]) if row else None

    def _experience_evidence(self, memory, query, limit):
        from .experience import text, urls
        scope = memory["scope"]
        pending, seen, evidence = [memory], set(), []
        while pending:
            current = pending.pop()
            if current["id"] in seen:
                continue
            seen.add(current["id"])
            meta = _object(current.get("meta"))
            refs = list(meta.get("source_experience_refs") or [])
            if meta.get("experience_ref"):
                refs.append(meta["experience_ref"])
            for ref in dict.fromkeys(refs):
                row = self.conn.execute(
                    "SELECT e.* FROM experiences e JOIN memories m ON m.id=e.memory_id "
                    "WHERE e.source_ref=? AND e.scope=? AND m.scope=? AND m.deleted=0", (ref, scope, scope),
                ).fetchone()
                if not row:
                    continue
                record = json.loads(row["record"])
                evidence.append({
                    "source_type": "experience", "source_id": row["memory_id"], "source_ref": ref,
                    "role": None, "actor_id": "assistant", "scope": scope,
                    "action": record.get("action"), "status": record.get("status"),
                    "args": record.get("args"), "urls": urls(record),
                    "content": _short_evidence(text(record.get("result", "")), query),
                    "ts": record.get("finished_at") or record.get("started_at"),
                    "autonomous": record.get("autonomous", False),
                })
            for ref in [*(meta.get("episode_ids") or []), *(meta.get("source_memory_ids") or [])]:
                linked = self.conn.execute("SELECT * FROM memories WHERE id=? AND scope=? AND deleted=0",
                                           (ref, scope)).fetchone()
                if linked:
                    pending.append(dict(linked))
        terms = set(_tokens(query))
        evidence.sort(key=lambda row: -len(terms.intersection(_tokens(row["content"]))))
        return list({row["source_ref"]: row for row in evidence}.values())[:limit]

    def sync(self, *, rebuild: bool = False, repair_fts: bool = True) -> Dict[str, int]:
        """Incrementally upsert/prune keys and repair FTS from current sources.

        Stats count keys, not source rows. Changed source text, scope, metadata or
        provenance invalidates its vector marks. No-op sync preserves them.
        """
        stats = dict(added=0, updated=0, deleted=0, unchanged=0, total=0, fts_repaired=0)
        with self._atomic():
            self.ensure_schema()
            if rebuild:
                self._queue_all_sources()
            dirty = set(tuple(r) for r in self.conn.execute("SELECT source_type,source_id FROM retrieval_dirty_sources"))
            pending = list(dirty)
            while pending:
                dependency = pending.pop()
                for dependent in self.conn.execute("SELECT dependent_type,dependent_id FROM retrieval_dependencies WHERE source_type=? AND source_id=?", dependency):
                    dependent = tuple(dependent)
                    if dependent not in dirty:
                        dirty.add(dependent)
                        pending.append(dependent)
            for source_type, source_id in sorted(dirty):
                if source_type not in {"memory", "message"}:
                    continue
                table, field = ("memories", "memory_id") if source_type == "memory" else ("messages", "message_id")
                raw = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (source_id,)).fetchone()
                existing = {r["id"]: dict(r) for r in self.conn.execute(f"SELECT * FROM retrieval_keys WHERE {field}=?", (source_id,))}
                seen = set()
                if raw:
                    row = dict(raw)
                    self._replace_dependencies(row, source_type)
                    keys = self._source_keys(row, source_type) if self._direct_source(row, source_type) else ()
                    for key in keys:
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
                else:
                    self.conn.execute("DELETE FROM retrieval_dependencies WHERE dependent_type=? AND dependent_id=?", (source_type, source_id))
                obsolete = set(existing) - seen
                self.conn.executemany("DELETE FROM retrieval_keys WHERE id = ?", ((key_id,) for key_id in obsolete))
                stats["deleted"] += len(obsolete)
            self.conn.executemany("DELETE FROM retrieval_dirty_sources WHERE source_type=? AND source_id=?", list(dirty))
            # Repair a missing, stale or partially rebuilt FTS without touching
            # healthy rows or the independently managed vector marks.
            needs_repair = repair_fts or rebuild or self.conn.execute("SELECT 1 FROM retrieval_sync_state WHERE key='repair_fts'").fetchone()
            removed = self.conn.execute(
                "DELETE FROM retrieval_keys_fts WHERE rowid IN ("
                "SELECT f.rowid FROM retrieval_keys_fts f LEFT JOIN retrieval_keys k ON k.rowid = f.rowid "
                "WHERE k.id IS NULL OR f.id IS NOT k.id OR f.tokens IS NOT k.tokens)"
            ).rowcount if needs_repair else 0
            inserted = self.conn.execute(
                "INSERT INTO retrieval_keys_fts(rowid, id, tokens) "
                "SELECT k.rowid, k.id, k.tokens FROM retrieval_keys k WHERE NOT EXISTS "
                "(SELECT 1 FROM retrieval_keys_fts f WHERE f.rowid = k.rowid)"
            ).rowcount if needs_repair else 0
            self.conn.execute("DELETE FROM retrieval_sync_state WHERE key='repair_fts'")
            total = self.conn.execute("SELECT COUNT(*) FROM retrieval_keys").fetchone()[0]
            stats.update(total=total, unchanged=total-stats["added"]-stats["updated"], fts_repaired=removed + inserted)
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
                    if self._direct_source(source, source_type):
                        sources[source_type, source["id"]] = (source["scope"], self._source_fingerprint(source, source_type))
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
                rows = cursor.fetchmany(_BATCH_SIZE if limit is None else min(_BATCH_SIZE, limit - len(result)))
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
        return list(self.reads.get("evidence_messages", (memory["id"], memory["scope"]),
                                   lambda: self._walk_memory_messages(memory)))

    def _walk_memory_messages(self, memory: Dict[str, Any]) -> List[Dict[str, Any]]:
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
            if clauses:
                for raw in self.conn.execute(
                    f"SELECT * FROM messages WHERE scope = ? AND ({' OR '.join(clauses)})", params,
                ):
                    message = dict(raw)
                    if _active(message, "message", include_archived=True):
                        messages[message["id"]] = message
            for ref in [*(meta.get("episode_ids") or []), *(meta.get("source_memory_ids") or [])]:
                linked = self.conn.execute("SELECT * FROM memories WHERE id=? AND scope=? AND deleted=0",
                                           (ref, scope)).fetchone()
                if linked and _active(dict(linked), "memory"):
                    pending.append(dict(linked))
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
            return self._source_evidence(source, source_type, query, limit)

    def source_evidence(self, memory_id, scopes, query="", limit=4):
        """Resolve a scoped cold source without adding it to the direct index."""
        scopes = _strings(scopes)
        if not scopes or limit <= 0:
            return []
        with self._atomic():
            row = self.conn.execute(
                f"SELECT * FROM memories WHERE id=? AND scope IN ({_placeholders(scopes)}) AND deleted=0",
                (memory_id, *scopes)).fetchone()
            if not row or not _active(dict(row), "memory"):
                return []
            graph = getattr(self.repo, "associations", None)
            node = graph.for_memory(memory_id) if graph else None
            if node and not graph.valid(node):
                return []
            return self._source_evidence(dict(row), "memory", query, limit)

    def _source_evidence(self, source, source_type, query, limit):
        experience_evidence = (self._experience_evidence(source, query, limit)
                               if source_type == "memory" else [])
        messages = [source] if source_type == "message" else self._memory_messages(source)
        if messages:
            terms = set(_tokens(query))
            messages.sort(key=lambda row: (
                -len(terms.intersection(_tokens(row["content"]))), row["ts"], row["id"],
            ))
            message_evidence = [{
                "source_type": "message", "source_id": row["id"],
                "source_ref": row.get("source_ref") or f"messages:{row['id']}",
                "role": row["role"], "actor_id": row["actor_id"], "content": _short_evidence(row["content"], query),
                "ts": row["ts"], "scope": row["scope"],
            } for row in messages[:limit]]
            combined = message_evidence + experience_evidence
            combined.sort(key=lambda ev: -len(terms.intersection(_tokens(ev["content"]))))
            return combined[:limit]
        if experience_evidence:
            return experience_evidence
        return [{
            "source_type": "legacy_memory" if source.get("origin") == ORIGIN_LEGACY else "summary",
            "source_id": source["id"],
            "source_ref": source.get("source_ref") or f"memories:{source['id']}",
            "role": None, "actor_id": None, "content": _short_evidence(source["content"], query),
            "ts": source["end_ts"] if source.get("end_ts") is not None else source.get("created_at"),
            "scope": source["scope"],
        }]
