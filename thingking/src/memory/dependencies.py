"""Canonical source references and a rebuildable reverse index of memory metadata.

Metadata and the messages.episode_id relation remain authoritative. This index
only answers which derived memories must be refreshed when a source changes.
"""
from __future__ import annotations

import json


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _values(value):
    return value if isinstance(value, (list, tuple, set)) else ()


def message_range(value):
    if isinstance(value, dict):
        value = [value.get("from_id", value.get("start")), value.get("to_id", value.get("end"))]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(isinstance(v, bool) or not str(v).isdigit() for v in value):
        return None
    start, end = map(int, value)
    return (start, end) if 0 < start <= end <= 2**63 - 1 else None


def direct_dependencies(repo, row, include_context=False):
    """Return scoped physical references, retaining unresolved aliases for late arrival.

For an attributed fact, its episode is context, not the evidence for every fact
in that episode. Retrieval may request that extra context dependency explicitly.
"""
    row = dict(row)
    meta, scope = _object(row.get("meta")), row["scope"]
    refs = set()
    ids = [*_values(meta.get("source_message_ids")), meta.get("fact_source_message_id")]
    for mid in ids:
        if not isinstance(mid, bool) and str(mid).isdigit() and 0 < int(mid) <= 2**63 - 1:
            refs.add(("message", str(int(mid))))
    for ref in _values(meta.get("source_message_refs")):
        if not isinstance(ref, str) or not ref:
            continue
        refs.add(("message_ref", ref))
        message = repo.conn.execute("SELECT id FROM messages WHERE source_ref=? AND scope=?", (ref, scope)).fetchone()
        if message:
            refs.add(("message", str(message[0])))
    parents = [*_values(meta.get("source_memory_ids")), *_values(meta.get("episode_ids"))]
    if include_context or not meta.get("fact_source_message_id"):
        parents.append(meta.get("episode_id"))
    refs.update(("memory", mid) for mid in parents if isinstance(mid, str) and mid)
    if row.get("kind") == "episode":
        refs.update(("message", str(r[0])) for r in repo.conn.execute(
            "SELECT id FROM messages WHERE episode_id=? AND scope=?", (row["id"], scope)))
    bounds = message_range(meta.get("message_range"))
    if bounds and row.get("conversation_id"):
        refs.add(("conversation", row["conversation_id"]))
        refs.update(("message", str(r[0])) for r in repo.conn.execute(
            "SELECT id FROM messages WHERE conversation_id=? AND scope=? AND id BETWEEN ? AND ?",
            (row["conversation_id"], scope, *bounds)))
    experiences = [*_values(meta.get("source_experience_refs")), meta.get("experience_ref")]
    for ref in experiences:
        if not isinstance(ref, str) or not ref:
            continue
        refs.add(("experience", ref))
        if repo.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='experiences'").fetchone():
            source = repo.conn.execute("SELECT memory_id FROM experiences WHERE source_ref=? AND scope=?", (ref, scope)).fetchone()
            if source:
                refs.add(("memory", source[0]))
    refs.discard(("memory", row["id"]))
    return refs


class SourceDependencies:
    def __init__(self, repo):
        self.repo, self.conn = repo, repo.conn
        with repo.transaction():
            existed = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_dependencies'").fetchone()
            self.conn.execute("""CREATE TABLE IF NOT EXISTS source_dependencies (
                scope TEXT NOT NULL,source_type TEXT NOT NULL,source_id TEXT NOT NULL,memory_id TEXT NOT NULL,
                PRIMARY KEY(scope,source_type,source_id,memory_id))""")
            self.conn.execute("CREATE INDEX IF NOT EXISTS source_dependency_memory ON source_dependencies(memory_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS source_dependency_source ON source_dependencies(source_type,source_id)")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS source_dependency_dirty (
                source_type TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(source_type,source_id))""")
            self.ensure_tracking()
            if not existed or not self.conn.execute("SELECT 1 FROM meta WHERE key='source_dependencies_v1'").fetchone():
                self.conn.execute("INSERT INTO source_dependency_dirty SELECT 'memory',id FROM memories WHERE true ON CONFLICT DO NOTHING")
                self.conn.execute("INSERT INTO meta VALUES('source_dependencies_v1','1') ON CONFLICT DO NOTHING")

    def ensure_tracking(self):
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        specs = [("memories", "id,meta,scope,kind,conversation_id,deleted", lambda a: [("memory", f"{a}.id")]),
                 ("messages", "id,source_ref,episode_id,scope,conversation_id", lambda a: [
                     ("message", f"CAST({a}.id AS TEXT)"), ("message_ref", f"{a}.source_ref"),
                     ("memory", f"{a}.episode_id"), ("conversation", f"{a}.conversation_id")])]
        if "experiences" in tables:
            specs.append(("experiences", "source_ref,memory_id,scope", lambda a: [("experience", f"{a}.source_ref")]))
        for table, columns, sources in specs:
            for event, suffix, aliases in (("INSERT", "ai", ("new",)), ("UPDATE OF " + columns, "au", ("old", "new")),
                                           ("DELETE", "ad", ("old",))):
                body = "".join("INSERT INTO source_dependency_dirty SELECT '%s',%s WHERE %s IS NOT NULL "
                               "ON CONFLICT DO NOTHING;" % (kind, value, value)
                               for alias in aliases for kind, value in sources(alias))
                self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS source_dependency_{table}_{suffix} AFTER {event} ON {table} BEGIN {body} END")

    def sync(self, limit=None):
        """Drain source changes atomically; no unrelated state/job writes are scanned."""
        processed = 0
        with self.repo.transaction():
            while limit is None or processed < limit:
                row = self.conn.execute("SELECT source_type,source_id FROM source_dependency_dirty ORDER BY rowid LIMIT 1").fetchone()
                if not row:
                    break
                kind, sid = row
                if kind == "memory":
                    self.conn.execute("DELETE FROM source_dependencies WHERE memory_id=?", (sid,))
                    memory = self.repo.get_memory(sid)
                    if memory and not memory["deleted"]:
                        self.conn.executemany("INSERT INTO source_dependencies VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                            [(memory["scope"], dep_type, dep_id, sid) for dep_type, dep_id in direct_dependencies(self.repo, memory)])
                else:
                    self.conn.execute("INSERT INTO source_dependency_dirty SELECT 'memory',memory_id FROM source_dependencies "
                                      "WHERE source_type=? AND source_id=? ON CONFLICT DO NOTHING", (kind, sid))
                self.conn.execute("DELETE FROM source_dependency_dirty WHERE source_type=? AND source_id=?", (kind, sid))
                processed += 1
        return processed

    def rebuild(self):
        """Reconstruct the projection from source metadata without altering sources."""
        with self.repo.transaction():
            self.conn.execute("DELETE FROM source_dependencies")
            self.conn.execute("INSERT INTO source_dependency_dirty SELECT 'memory',id FROM memories WHERE true ON CONFLICT DO NOTHING")
            return self.sync()

    def descendants(self, scope, sources):
        self.sync()
        pending = {(kind, str(sid)) for kind, sid in sources if sid is not None}
        # Resolve aliases at the boundary; the reverse index also retains aliases
        # so a dependency recorded before its original message arrives is found.
        for kind, sid in list(pending):
            if kind == "message":
                row = self.conn.execute("SELECT source_ref FROM messages WHERE id=? AND scope=?", (sid, scope)).fetchone()
                if row and row[0]:
                    pending.add(("message_ref", row[0]))
            elif kind == "memory":
                row = self.repo.get_memory(sid)
                if row and row["scope"] == scope and row["meta"].get("experience_ref"):
                    pending.add(("experience", row["meta"]["experience_ref"]))
        roots = {sid for kind, sid in pending if kind == "memory"}
        seen, found = set(), set()
        while pending:
            kind, sid = pending.pop()
            if (kind, sid) in seen:
                continue
            seen.add((kind, sid))
            for row in self.conn.execute("SELECT d.memory_id FROM source_dependencies d JOIN memories m ON m.id=d.memory_id "
                                         "WHERE d.scope=? AND d.source_type=? AND d.source_id=? AND m.scope=? AND m.deleted=0",
                                         (scope, kind, sid, scope)):
                mid = row[0]
                found.add(mid)
                pending.add(("memory", mid))
        return found - roots
