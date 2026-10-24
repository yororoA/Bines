"""Conservative event identities, separate from immutable physical evidence roots."""
from __future__ import annotations

import hashlib
import json
import re

_JOURNAL = re.compile(r"^experience:([0-9a-f]{64}):(.+):([^:]+)$")


class ProvenanceStore:
    def __init__(self, repo):
        self.repo, self.conn = repo, repo.conn
        self.conn.execute("""CREATE TABLE IF NOT EXISTS evidence_events (
            scope TEXT NOT NULL, root TEXT NOT NULL, event_id TEXT NOT NULL,
            PRIMARY KEY(scope,root))""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS evidence_event_siblings ON evidence_events(scope,event_id)")
        if not self.conn.execute("SELECT 1 FROM meta WHERE key='evidence_events_v1'").fetchone():
            for row in self.conn.execute("SELECT source_ref,scope,record FROM experiences").fetchall():
                self.register(row["scope"], row["source_ref"], json.loads(row["record"]))
            self.conn.execute("INSERT INTO meta VALUES('evidence_events_v1','1')")

    def register(self, scope, source_ref, record):
        match = _JOURNAL.fullmatch(source_ref)
        # Only a runtime journal namespace plus its exact round identity is
        # accepted. URLs, topics and long-running activity IDs are not identity.
        if not match or match[2] != record.get("turn_id"):
            return None
        event_id = "event:" + hashlib.sha256(
            json.dumps([scope, match[1], match[2]]).encode()).hexdigest()
        roots = ["experience:" + source_ref]
        if record.get("category") == "turn":
            for ref in record.get("turn_message_refs") or []:
                row = self.conn.execute("SELECT id FROM messages WHERE scope=? AND source_ref=?",
                                        (scope, ref)).fetchone()
                if row:
                    roots.append(f"message:{row[0]}")
        changed = []
        for root in roots:
            inserted = self.conn.execute("INSERT OR IGNORE INTO evidence_events VALUES(?,?,?)", (scope, root, event_id))
            if inserted.rowcount:
                changed.append(root)
        graph = getattr(self.repo, "associations", None)
        if changed and graph:
            ids = [r[0] for r in self.conn.execute("""SELECT DISTINCT n.id
                FROM association_nodes n,json_each(n.family) f WHERE n.scope=? AND f.value IN ("""
                + ",".join("?" for _ in changed) + ")", (scope, *changed))]
            for nid in ids:
                graph.invalidate(nid)
                for edge in self.conn.execute("""SELECT * FROM association_edges WHERE confidence>0
                    AND (source_node=? OR target_node=?)""", (nid, nid)).fetchall():
                    a, b = graph.get(edge["source_node"]), graph.get(edge["target_node"])
                    if a and b and not graph.independent(a, b):
                        self.conn.execute("UPDATE association_edges SET confidence=0 WHERE id=?", (edge["id"],))
        return event_id

    def family(self, scope, roots):
        roots = sorted(set(roots))
        if not roots:
            return set()
        rows = self.conn.execute(
            "SELECT root,event_id FROM evidence_events WHERE scope=? AND root IN ("
            + ",".join("?" for _ in roots) + ")", (scope, *roots))
        canonical = {r["root"]: r["event_id"] for r in rows}
        return {canonical.get(root, root) for root in roots}

    def siblings(self, node, graph, limit=20):
        family = self.family(node["scope"], node["family"])
        roots = [r[0] for r in self.conn.execute(
            "SELECT root FROM evidence_events WHERE scope=? AND event_id IN ("
            + ",".join("?" for _ in family) + ")", (node["scope"], *sorted(family)))] if family else []
        if not roots:
            return []
        rows = self.conn.execute("""SELECT DISTINCT n.id FROM association_nodes n,json_each(n.family) f
            WHERE n.scope=? AND n.superseded=0 AND n.id!=? AND f.value IN ("""
            + ",".join("?" for _ in roots) + ") LIMIT ?", (node["scope"], node["id"], *roots, limit))
        return [graph.get(r[0], node["scope"]) for r in rows]
