"""Scoped question attention learned exclusively from validated citation receipts."""
from __future__ import annotations

import json
import uuid

from .associations import digest
from .lexical_index import query_terms


def context_key(query):
    return digest(sorted(query_terms(query)) or [query.strip().lower()])


class CompetitionStore:
    def __init__(self, repo):
        self.repo, self.conn = repo, repo.conn
        self.conn.execute("""CREATE TABLE IF NOT EXISTS retrieval_contexts (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, query_key TEXT NOT NULL,
            snapshot TEXT NOT NULL, created_at REAL NOT NULL)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS context_chain_credit (
            scope TEXT NOT NULL, query_key TEXT NOT NULL, chain_id TEXT NOT NULL,
            cited INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(scope,query_key,chain_id))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS context_credit_events (
            id TEXT PRIMARY KEY, context_id TEXT NOT NULL, scope TEXT NOT NULL,
            turn_id TEXT NOT NULL, family TEXT NOT NULL, chain_id TEXT NOT NULL,
            created_at REAL NOT NULL)""")
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(context_chain_credit)")}
        if "reason_version" not in columns:
            self.conn.execute("ALTER TABLE context_chain_credit ADD COLUMN reason_version TEXT NOT NULL DEFAULT ''")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS context_feedback (
            scope TEXT NOT NULL, query_key TEXT NOT NULL, chain_id TEXT NOT NULL,
            reason_version TEXT NOT NULL, turn_id TEXT NOT NULL, family TEXT NOT NULL,
            event_type TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(scope,turn_id,family,chain_id,event_type))""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS context_feedback_query ON context_feedback(scope,query_key,chain_id)")
        self.conn.create_function("memory_decay", 2, lambda age, half_life: 2.0 ** (-max(0.0, age) / half_life))

    def shares(self, scope, query, chain_ids):
        ids = sorted(set(chain_ids))
        key = context_key(query)
        rejected = {r["chain_id"]: min(.9, .3 * r["rejected"])
                  for r in self.conn.execute(
                      "SELECT * FROM context_chain_credit WHERE scope=? AND query_key=?", (scope, key))
                  if r["reason_version"] == self.repo.associations.reason_version(r["chain_id"], scope)}
        half_life = max(1.0, getattr(self.repo.associations.settings, "usage_half_life_days", 30)) * 86400
        feedback = {}
        for row in self.conn.execute("""SELECT chain_id,reason_version,event_type,
                SUM(memory_decay(?-created_at,?)) AS weight FROM context_feedback
                WHERE scope=? AND query_key=? GROUP BY chain_id,reason_version,event_type""",
                (self.repo.clock(), half_life, scope, key)):
            if row["reason_version"] == self.repo.associations.reason_version(row["chain_id"], scope):
                feedback.setdefault(row["chain_id"], {})[row["event_type"]] = row["weight"]
        weights = {}
        for cid in ids:
            values = feedback.get(cid, {})
            cited = values.get("cited", 0.0)
            reward = .9 * cited / (max(cited, values.get("presented", 0.0)) + 2.0)
            weights[cid] = max(.1, 1.0 + reward - rejected.get(cid, 0.0))
        total = sum(weights.values())
        return {cid: value / total for cid, value in weights.items()} if total else {}

    def _feedback(self, context, turn_id, node, chain_id, event_type):
        family = json.dumps(sorted(self.repo.associations.evidence_family(node)))
        self.conn.execute("INSERT OR IGNORE INTO context_feedback VALUES(?,?,?,?,?,?,?,?)",
            (node["scope"], context["query_key"], chain_id,
             self.repo.associations.reason_version(chain_id, node["scope"]), turn_id,
             family, event_type, self.repo.clock()))

    def observe(self, scope, turn_id, node, presented):
        """Record only an actually displayed receipt, not a search result."""
        row = self.conn.execute("SELECT * FROM retrieval_contexts WHERE id=? AND scope=?",
            (presented.get("context_id"), scope)).fetchone()
        if not row or row["created_at"] < self.repo.clock() - 7 * 86400:
            return
        snapshot = json.loads(row["snapshot"])
        offered = snapshot["nodes"].get(node["id"], {})
        if offered.get("digest") != node["digest"]:
            return
        for cid in set(presented.get("chain_ids") or []).intersection(offered.get("chains", [])):
            if snapshot["revisions"].get(cid) == self.repo.associations.reason_version(cid, scope):
                self._feedback(row, turn_id, node, cid, "presented")

    def mint(self, query, rows):
        for scope in sorted({r["scope"] for r in rows}):
            offered = {}
            revisions = {}
            for row in rows:
                if row["scope"] != scope:
                    continue
                ids = row.get("chain_ids") or []
                offered[row["node_id"]] = {"digest": row["node_digest"], "chains": ids}
                for cid in ids:
                    version = self.repo.associations.reason_version(cid, scope)
                    if version:
                        revisions[cid] = version
            snapshot = {"nodes": offered, "revisions": revisions, "query": query[:400]}
            cid = "ctx:" + uuid.uuid4().hex
            self.conn.execute("INSERT INTO retrieval_contexts VALUES(?,?,?,?,?)",
                              (cid, scope, context_key(query), json.dumps(snapshot), self.repo.clock()))
            shares = self.shares(scope, query, revisions)
            for row in rows:
                if row["scope"] == scope:
                    row["context_id"] = cid
                    row["reason_shares"] = {chain: shares[chain] for chain in row.get("chain_ids") or []}
            # Receipts are meaningful for at most seven days; bound snapshot storage.
            self.conn.execute("DELETE FROM retrieval_contexts WHERE created_at<?", (self.repo.clock() - 7 * 86400,))
            self.conn.execute("""DELETE FROM retrieval_contexts WHERE id IN (
                SELECT id FROM retrieval_contexts ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET 4000)""")
        return rows

    def history(self, node_id, scope):
        rows = self.conn.execute("""SELECT * FROM retrieval_contexts WHERE scope=?
            AND json_extract(snapshot,'$.nodes') LIKE ? ORDER BY created_at DESC LIMIT 80""",
            (scope, f"%{node_id}%"))
        seen, result = set(), []
        for row in rows:
            snapshot = json.loads(row["snapshot"])
            query = snapshot.get("query")
            if not query or node_id not in snapshot["nodes"] or row["query_key"] in seen:
                continue
            seen.add(row["query_key"])
            result.append({"context_id": row["id"], "query": query, "created_at": row["created_at"],
                           "shares": self.shares(scope, query, snapshot["revisions"])})
            if len(result) >= 8:
                break
        return result

    def learn(self, scope, turn_id, node, presented, citation):
        context_id, chain_id = presented.get("context_id"), citation.get("chain_id")
        if not context_id or not chain_id or not citation.get("source_quote"):
            return False
        row = self.conn.execute("SELECT * FROM retrieval_contexts WHERE id=? AND scope=?",
                                (context_id, scope)).fetchone()
        if not row or row["created_at"] < self.repo.clock() - 7 * 86400:
            return False
        snapshot = json.loads(row["snapshot"])
        offered = snapshot["nodes"].get(node["id"]) or {}
        current = self.repo.associations.reason_version(chain_id, scope)
        if (offered.get("digest") != node["digest"] or chain_id not in offered.get("chains", [])
                or not current or current != snapshot["revisions"].get(chain_id)):
            return False
        family = sorted(self.repo.associations.evidence_family(node))
        key = digest([scope, turn_id, family])
        inserted = self.conn.execute("INSERT OR IGNORE INTO context_credit_events VALUES(?,?,?,?,?,?,?)",
            (key, context_id, scope, turn_id, json.dumps(family), chain_id, self.repo.clock())).rowcount
        if inserted:
            self._feedback(row, turn_id, node, chain_id, "cited")
            self.conn.execute("""INSERT INTO context_chain_credit VALUES(?,?,?,1,0,?)
                ON CONFLICT(scope,query_key,chain_id) DO UPDATE SET
                  cited=CASE WHEN reason_version=excluded.reason_version THEN MIN(10000,cited+1) ELSE 1 END,
                  rejected=CASE WHEN reason_version=excluded.reason_version THEN rejected ELSE 0 END,
                  reason_version=excluded.reason_version""", (scope, row["query_key"], chain_id, current))
        return bool(inserted)

    def reject(self, scope, chain_id, context_id):
        context = self.conn.execute("SELECT * FROM retrieval_contexts WHERE id=? AND scope=?",
                                    (context_id, scope)).fetchone()
        if context and chain_id in json.loads(context["snapshot"])["revisions"]:
            self.conn.execute("""INSERT INTO context_chain_credit VALUES(?,?,?,0,1,?)
                ON CONFLICT(scope,query_key,chain_id) DO UPDATE SET
                  rejected=CASE WHEN reason_version=excluded.reason_version THEN MIN(10000,rejected+1) ELSE 1 END,
                  cited=CASE WHEN reason_version=excluded.reason_version THEN cited ELSE 0 END,
                  reason_version=excluded.reason_version""",
                (scope, context["query_key"], chain_id, self.repo.associations.reason_version(chain_id, scope)))
