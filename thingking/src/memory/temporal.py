"""Opt-in temporal storage on a MemoryRepository connection.

Times are finite Unix seconds; ``as_of`` is effective time, not "known as of"
recording time. Fact intervals are [valid_from, valid_to). An omitted as_of
uses repo.clock(). No old facts are inferred from existing memory text.

Each fact version needs a distinct parent memory_id: returning a memory that
contains both expired and current facts could leak the expired text. source_ref
must be an opaque, scope-local identifier (never sensitive prose). It may name
a memory id, a memories.source_ref, or an external source. Resolvable memory
sources must be alive and in the exact same scope. Shared source identifiers
form one retraction unit, including sibling facts and resolvable source aliases.
Create a parent and its fact in the same repo.transaction() so rejected facts
cannot leave behind a newly created unversioned memory.

Deletion only changes these temporal tables, not memories, state, messages,
indexes, backups, or SQLite free pages/WAL. Integrators must soft-delete the
parent and clean current state/indexes in the same repo.transaction(), calling
this store before any hard deletion. Full snapshots lack field-level lineage:
we redact the linked version and all later versions in that scope. Unlinked
snapshots/baselines cannot be traced to a source; new snapshots must be built
from cleaned current state. Versions must follow the repository's increasing
state-version convention. There is no implicit group/member scope expansion.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from .repository import MemoryRepository, StateConflict


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS state_history (
        scope TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 0),
        data TEXT,
        recorded_at REAL NOT NULL,
        effective_at REAL NOT NULL,
        source_ref TEXT,
        source_memory_id TEXT,
        is_baseline INTEGER NOT NULL DEFAULT 0,
        redacted INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (scope, version),
        CHECK ((redacted = 0 AND data IS NOT NULL) OR (redacted = 1 AND data IS NULL))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_state_history_effective
        ON state_history(scope, effective_at, version)""",
    """CREATE INDEX IF NOT EXISTS idx_state_history_source
        ON state_history(scope, source_ref)""",
    """CREATE TABLE IF NOT EXISTS fact_versions (
        memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
        scope TEXT NOT NULL,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        value TEXT NOT NULL,
        valid_from REAL NOT NULL,
        valid_to REAL,
        recorded_at REAL NOT NULL,
        source_memory_id TEXT,
        source_ref TEXT NOT NULL,
        UNIQUE (scope, subject, predicate, valid_from),
        CHECK (valid_to IS NULL OR valid_to > valid_from)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_fact_versions_source
        ON fact_versions(scope, source_ref)""",
    """CREATE INDEX IF NOT EXISTS idx_fact_versions_source_memory
        ON fact_versions(source_memory_id)""",
    """CREATE TABLE IF NOT EXISTS temporal_retractions (
        scope TEXT NOT NULL,
        source_ref TEXT NOT NULL,
        fact_key TEXT,
        valid_from REAL,
        PRIMARY KEY (scope, source_ref)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_temporal_retractions_boundary
        ON temporal_retractions(scope, fact_key, valid_from)""",
)


def _timestamp(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite Unix seconds")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be finite Unix seconds") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite Unix seconds")
    return result


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fact_key(scope: str, subject: str, predicate: str) -> str:
    return hashlib.sha256(_json([scope, subject, predicate]).encode("utf-8")).hexdigest()


def _limit(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("limit must be a nonnegative integer")
    return value


def _source_visible(alias: str) -> str:
    # Aliases are internal constants, never caller-supplied SQL.
    return (
        f"NOT EXISTS (SELECT 1 FROM temporal_retractions r "
        f"WHERE r.scope = {alias}.scope AND r.source_ref = {alias}.source_ref) "
        f"AND ({alias}.source_memory_id IS NULL OR EXISTS (SELECT 1 FROM memories src "
        f"WHERE src.id = {alias}.source_memory_id AND src.scope = {alias}.scope AND src.deleted = 0 "
        "AND NOT EXISTS (SELECT 1 FROM temporal_retractions r WHERE r.scope = src.scope "
        "AND (r.source_ref = src.id OR r.source_ref = src.source_ref))))"
    )


_FACT_VISIBLE = (
    "m.deleted = 0 AND m.scope = f.scope AND json_extract(m.meta,'$.repair_pending') IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM temporal_retractions r WHERE r.scope = m.scope "
    "AND (r.source_ref = m.id OR r.source_ref = m.source_ref)) AND " + _source_visible("f")
)


class TemporalStore:
    """Independent, single-writer companion; does not own or close repo.conn.

    Writes return True when inserted/changed and False for an exact retry/no-op.
    Reusing an immutable key with different data raises StateConflict; invalid
    parents, sources, or arguments raise ValueError. Reads return decoded dicts.
    The repository connection must be serialized by its owning Memory Server.
    Initialize once before integrating writes. A scope without history gets a
    baseline at state.updated_at, recorded now, unless it has any retractions.
    """

    def __init__(self, repo: MemoryRepository):
        self.repo = repo
        with repo.transaction() as conn:
            # executescript would implicitly commit an enclosing transaction.
            for statement in _SCHEMA:
                conn.execute(statement)
            rows = conn.execute(
                "SELECT s.* FROM state s WHERE NOT EXISTS "
                "(SELECT 1 FROM state_history h WHERE h.scope = s.scope) AND NOT EXISTS "
                "(SELECT 1 FROM temporal_retractions r WHERE r.scope = s.scope)"
            ).fetchall()
            recorded_at = _timestamp(repo.clock(), "recorded_at")
            for row in rows:
                conn.execute(
                    "INSERT INTO state_history(scope, version, data, recorded_at, effective_at, is_baseline) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (row["scope"], row["version"], row["data"], recorded_at, row["updated_at"]),
                )

    def _as_of(self, value: Optional[float]) -> float:
        return _timestamp(self.repo.clock() if value is None else value, "as_of")

    def _check_source(self, scope: str, source_ref: Optional[str]) -> Optional[str]:
        if source_ref is None:
            return None
        _text(source_ref, "source_ref")
        if self.repo.conn.execute(
            "SELECT 1 FROM temporal_retractions WHERE scope = ? AND source_ref = ?", (scope, source_ref),
        ).fetchone():
            raise ValueError("source has been retracted")
        rows = self.repo.conn.execute(
            "SELECT id, scope, deleted, source_ref FROM memories WHERE id = ? OR source_ref = ?",
            (source_ref, source_ref),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous memory source_ref")
        if not rows:
            return None
        source = rows[0]
        if source["scope"] != scope or source["deleted"]:
            raise ValueError("source memory must be active in the same scope")
        if self.repo.conn.execute(
            "SELECT 1 FROM temporal_retractions WHERE scope = ? AND source_ref IN (?, ?)",
            (scope, source["id"], source["source_ref"]),
        ).fetchone():
            raise ValueError("source memory has been retracted")
        return source["id"]

    def snapshot_state(
        self, scope: str, version: int, data: Dict[str, Any], recorded_at: float,
        effective_at: Optional[float] = None, source_ref: Optional[str] = None,
    ) -> bool:
        """Append an immutable (scope, version) snapshot; never update state.

        effective_at defaults to recorded_at. Identical JSON object key order
        is immaterial; all other supplied fields must match on a retry.
        Missing versions inside the redacted range cannot be backfilled.
        """
        _text(scope, "scope")
        if type(version) is not int or version < 0:
            raise ValueError("version must be a nonnegative integer")
        if not isinstance(data, dict):
            raise ValueError("state data must be an object")
        encoded = _json(data)
        recorded_at = _timestamp(recorded_at, "recorded_at")
        effective_at = recorded_at if effective_at is None else _timestamp(effective_at, "effective_at")
        with self.repo.transaction() as conn:
            source_memory_id = self._check_source(scope, source_ref)
            old = conn.execute(
                "SELECT * FROM state_history WHERE scope = ? AND version = ?", (scope, version),
            ).fetchone()
            if old:
                if (old["redacted"] or _json(json.loads(old["data"])) != encoded
                        or old["recorded_at"] != recorded_at or old["effective_at"] != effective_at
                        or old["source_ref"] != source_ref):
                    raise StateConflict("state history version already exists or was redacted")
                return False
            redacted = conn.execute(
                "SELECT MIN(version) AS first, MAX(version) AS last FROM state_history "
                "WHERE scope = ? AND redacted = 1", (scope,),
            ).fetchone()
            if redacted["first"] is not None and redacted["first"] <= version <= redacted["last"]:
                raise StateConflict("state history version belongs to a redacted range")
            conn.execute(
                "INSERT INTO state_history(scope, version, data, recorded_at, effective_at, source_ref, "
                "source_memory_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope, version, encoded, recorded_at, effective_at, source_ref, source_memory_id),
            )
        return True

    def state_history(
        self, scope: str, as_of: Optional[float] = None, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return snapshots effective by as_of, newest effective time first.

        Redacted rows are omitted. A known deleted source also hides subsequent
        versions before explicit cleanup, since snapshots can inherit its data.
        """
        _text(scope, "scope")
        rows = self.repo.conn.execute(
            "SELECT h.* FROM state_history h WHERE h.scope = ? AND h.effective_at <= ? AND h.redacted = 0 "
            "AND NOT EXISTS (SELECT 1 FROM state_history prior WHERE prior.scope = h.scope "
            f"AND prior.version <= h.version AND NOT ({_source_visible('prior')})) "
            "ORDER BY h.effective_at DESC, h.version DESC LIMIT ?",
            (scope, self._as_of(as_of), _limit(limit)),
        ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"]), "is_baseline": bool(row["is_baseline"]),
                 "redacted": False} for row in rows]

    def put_fact(
        self, memory_id: str, scope: str, subject: str, predicate: str, value: Any,
        valid_from: float, source_ref: Optional[str] = None,
    ) -> bool:
        """Append one immutable fact per parent memory; close its predecessor.

        Out-of-order inserts stop at the next known transition, including a
        retracted transition. Two different memories at the same key/time are
        ambiguous and raise StateConflict. recorded_at is assigned only once.
        """
        for name, text in (("memory_id", memory_id), ("scope", scope), ("subject", subject), ("predicate", predicate)):
            _text(text, name)
        valid_from = _timestamp(valid_from, "valid_from")
        encoded = _json(value)
        with self.repo.transaction() as conn:
            parent = conn.execute("SELECT scope, deleted, source_ref FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if not parent or parent["deleted"] or parent["scope"] != scope:
                raise ValueError("parent memory must be active in the same scope")
            self._check_source(scope, memory_id)
            self._check_source(scope, parent["source_ref"])
            source_ref = source_ref if source_ref is not None else (parent["source_ref"] or memory_id)
            source_memory_id = self._check_source(scope, source_ref)
            old = conn.execute("SELECT * FROM fact_versions WHERE memory_id = ?", (memory_id,)).fetchone()
            if old:
                if (old["scope"], old["subject"], old["predicate"], old["value"], old["valid_from"], old["source_ref"]) != (
                    scope, subject, predicate, encoded, valid_from, source_ref,
                ):
                    raise StateConflict("memory already has a different fact version")
                return False
            key = (scope, subject, predicate)
            if conn.execute(
                "SELECT 1 FROM fact_versions WHERE scope = ? AND subject = ? AND predicate = ? AND valid_from = ?",
                (*key, valid_from),
            ).fetchone():
                raise StateConflict("fact key already has a version at valid_from")
            next_row = conn.execute(
                "SELECT MIN(valid_from) AS boundary FROM ("
                "SELECT valid_from FROM fact_versions WHERE scope = ? AND subject = ? AND predicate = ? "
                "AND valid_from > ? UNION ALL "
                "SELECT valid_from FROM temporal_retractions WHERE scope = ? AND fact_key = ? AND valid_from > ?)",
                (*key, valid_from, scope, _fact_key(*key), valid_from),
            ).fetchone()
            conn.execute(
                "INSERT INTO fact_versions(memory_id, scope, subject, predicate, value, valid_from, valid_to, "
                "recorded_at, source_memory_id, source_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, *key, encoded, valid_from, next_row["boundary"],
                 _timestamp(self.repo.clock(), "recorded_at"), source_memory_id, source_ref),
            )
            # Never extend a predecessor across a gap left by a retraction.
            conn.execute(
                "UPDATE fact_versions SET valid_to = ? WHERE scope = ? AND subject = ? AND predicate = ? "
                "AND valid_from < ? AND (valid_to IS NULL OR valid_to > ?)",
                (valid_from, *key, valid_from, valid_from),
            )
        return True

    def visible_memory_ids(
        self, memory_ids: Sequence[str], as_of: Optional[float] = None, include_history: bool = False,
    ) -> List[str]:
        """Filter pre-authorized candidates, preserving order and deduplicating.

        This is not an authorization API: callers must scope candidate IDs.
        include_history includes ended versions but never future or withdrawn
        ones. Unversioned legacy memories pass from created_at onward; no fact
        validity is inferred for them.
        """
        when = self._as_of(as_of)
        ids = list(dict.fromkeys(memory_ids))
        visible = set()
        interval = "" if include_history else "AND (f.valid_to IS NULL OR ? < f.valid_to)"
        for offset in range(0, len(ids), 400):
            chunk = ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.repo.conn.execute(
                f"SELECT m.id FROM memories m WHERE m.id IN ({placeholders}) AND m.deleted = 0 "
                "AND NOT EXISTS (SELECT 1 FROM temporal_retractions r WHERE r.scope = m.scope "
                "AND (r.source_ref = m.id OR r.source_ref = m.source_ref)) "
                "AND ((NOT EXISTS (SELECT 1 FROM fact_versions f WHERE f.memory_id = m.id) AND m.created_at <= ?) "
                "OR EXISTS (SELECT 1 FROM fact_versions f WHERE f.memory_id = m.id "
                f"AND {_FACT_VISIBLE} AND f.valid_from <= ? {interval}))",
                (*chunk, when, when, *((when,) if not include_history else ())),
            ).fetchall()
            visible.update(row["id"] for row in rows)
        return [memory_id for memory_id in ids if memory_id in visible]

    def facts(
        self, scope: str, subject: Optional[str] = None, predicate: Optional[str] = None,
        as_of: Optional[float] = None, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return facts valid at as_of for an exact scope and optional key."""
        _text(scope, "scope")
        when = self._as_of(as_of)
        sql = (
            "SELECT f.* FROM fact_versions f JOIN memories m ON m.id = f.memory_id "
            f"WHERE f.scope = ? AND {_FACT_VISIBLE} "
            "AND f.valid_from <= ? AND (f.valid_to IS NULL OR ? < f.valid_to)"
        )
        params: List[Any] = [scope, when, when]
        for column, value in (("subject", subject), ("predicate", predicate)):
            if value is not None:
                _text(value, column)
                sql += f" AND f.{column} = ?"
                params.append(value)
        rows = self.repo.conn.execute(
            sql + " ORDER BY f.valid_from DESC, f.memory_id LIMIT ?", (*params, _limit(limit)),
        ).fetchall()
        result = []
        corrections = getattr(self.repo, "corrections", None)
        for row in rows:
            value = json.loads(row["value"])
            node = self.repo.associations.for_memory(row["memory_id"]) if corrections else None
            history = corrections.inherited_facts(node) if node else []
            suppressed = False
            for correction in history:
                if isinstance(value, str):
                    if correction["quote"] in value:
                        value = value.replace(correction["quote"], correction["replacement"])
                    elif value and value in correction["quote"]:
                        suppressed = True  # Partial sentence corrections cannot infer a scalar value.
                elif str(value) in correction["quote"] or correction["quote"] in json.dumps(value, ensure_ascii=False):
                    suppressed = True
            if not suppressed:
                result.append({**dict(row), "value": value, **({"corrections": history} if history else {})})
        return result

    def delete_memory(self, memory_id: str) -> bool:
        """Purge a memory's facts and linked descendants, without reviving old facts.

        Also redact whole linked state snapshots and later versions in the same
        scope. Content-free retractions prevent retries and retain fact interval
        boundaries. Call before hard deletion; a missing parent is a no-op.
        """
        _text(memory_id, "memory_id")
        with self.repo.transaction() as conn:
            parent = conn.execute("SELECT scope, source_ref FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if parent is None:
                return False
            scope = parent["scope"]
            return self._retract(conn, scope, {memory_id} | ({parent["source_ref"]} if parent["source_ref"] else set()))

    def retract_source(self, scope: str, source_ref: str) -> bool:
        """撤回一条消息/来源时清除它产生的状态快照，保留无内容防重放记录。"""
        _text(scope, "scope")
        _text(source_ref, "source_ref")
        with self.repo.transaction() as conn:
            return self._retract(conn, scope, {source_ref})

    def _retract(self, conn: sqlite3.Connection, scope: str, roots: set[str]) -> bool:
        pending = set(roots)
        refs = set()
        removed: Dict[str, sqlite3.Row] = {}
        while pending:
            ref = pending.pop()
            refs.add(ref)
            sources = conn.execute(
                "SELECT id, source_ref FROM memories WHERE scope = ? AND (id = ? OR source_ref = ?)",
                (scope, ref, ref),
            ).fetchall()
            for source in sources:
                for linked in (source["id"], source["source_ref"]):
                    if linked and linked not in refs:
                        pending.add(linked)
            rows = conn.execute(
                "SELECT f.*, m.source_ref AS parent_source_ref FROM fact_versions f "
                "JOIN memories m ON m.id = f.memory_id WHERE f.scope = ? AND "
                "(f.memory_id = ? OR f.source_memory_id = ? OR f.source_ref = ? OR m.source_ref = ?)",
                (scope, ref, ref, ref, ref),
            ).fetchall()
            for row in rows:
                removed[row["memory_id"]] = row
                for linked in (row["memory_id"], row["source_memory_id"], row["source_ref"],
                               row["parent_source_ref"]):
                    if linked and linked not in refs:
                        pending.add(linked)
        changed = False
        first_version = None
        for ref in refs:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO temporal_retractions(scope, source_ref) VALUES (?, ?)", (scope, ref),
            )
            changed = changed or bool(cursor.rowcount)
            row = conn.execute(
                "SELECT MIN(version) AS version FROM state_history WHERE scope = ? AND "
                "(source_ref = ? OR source_memory_id = ?)", (scope, ref, ref),
            ).fetchone()
            if row["version"] is not None:
                first_version = row["version"] if first_version is None else min(first_version, row["version"])
        for row in removed.values():
            conn.execute(
                "UPDATE temporal_retractions SET fact_key = ?, valid_from = ? WHERE scope = ? AND source_ref = ?",
                (_fact_key(scope, row["subject"], row["predicate"]), row["valid_from"], scope, row["memory_id"]),
            )
            conn.execute("DELETE FROM fact_versions WHERE memory_id = ?", (row["memory_id"],))
        if first_version is not None:
            conn.execute(
                "UPDATE state_history SET data = NULL, source_ref = NULL, source_memory_id = NULL, redacted = 1 "
                "WHERE scope = ? AND version >= ?", (scope, first_version),
            )
        return changed or bool(removed) or first_version is not None
