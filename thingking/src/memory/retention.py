"""Frozen consolidation decisions and recoverable hot/cold lifecycle."""
from __future__ import annotations

import json

from .associations import digest

POLICY_VERSION = "association-topk-v1"


class RetentionStore:
    def __init__(self, repo, graph, settings):
        self.repo, self.conn, self.graph, self.settings = repo, repo.conn, graph, settings
        self.conn.execute("""CREATE TABLE IF NOT EXISTS consolidation_snapshots (
            job_id INTEGER PRIMARY KEY, scope TEXT NOT NULL, policy TEXT NOT NULL,
            graph_waterline INTEGER NOT NULL, source_hash TEXT NOT NULL,
            data TEXT NOT NULL, created_at REAL NOT NULL, applied INTEGER NOT NULL DEFAULT 0)""")

    def get(self, job_id):
        row = self.conn.execute("SELECT * FROM consolidation_snapshots WHERE job_id=?", (job_id,)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def source_hash(self, sources):
        return digest([[r["id"], r["scope"], r["content"], r.get("role"), r.get("actor_id"),
                        r.get("source_ref"), r.get("meta")] for r in sources])

    def freeze(self, job, sources, nodes, k):
        old = self.get(job["id"])
        if old:
            return old if old["source_hash"] == self.source_hash(sources) else None
        node_ids = [n["id"] for n in nodes]
        # Only this batch's sources must be ready. Unrelated historical backfill
        # cannot indefinitely hold up a new conversation's frozen decision.
        waiting = self.conn.execute("""SELECT 1 FROM association_nodes n JOIN jobs j
            ON json_extract(j.payload,'$.node_id')=n.id
            AND json_extract(j.payload,'$.revision')=n.revision
            WHERE n.scope=? AND n.ready=0 AND n.superseded=0
            AND j.kind='associate' AND j.status IN ('pending','running') AND n.id IN ("""
            + ",".join("?" for _ in node_ids) + ") LIMIT 1",
            (job["scope"], *node_ids)).fetchone() if node_ids else None
        if waiting:
            return False
        scores, seen, ranked = {}, set(), []
        for node in nodes:
            if not self.graph.valid(node):
                continue
            scores[node["id"]] = self.graph.strength(node)
            ranked.append(node)
        ranked.sort(key=lambda n: (-scores[n["id"]], -n["prior"], n["seq"], n["id"]))
        independent = []
        for node in ranked:
            family = self.graph.evidence_family(node)
            if seen.intersection(family):
                continue
            seen.update(family)
            independent.append(node)
        data = {"source_ids": [s["id"] for s in sources], "node_ids": [n["id"] for n in nodes],
                "node_versions": {n["id"]: n["digest"] for n in nodes},
                "scores": scores, "protected": [n["id"] for n in independent[:k]],
                "association_incomplete": [n["id"] for n in nodes if not n["ready"]]}
        self.conn.execute("INSERT INTO consolidation_snapshots VALUES(?,?,?,?,?,?,?,0)",
                          (job["id"], job["scope"], POLICY_VERSION, self.graph.waterline(),
                           self.source_hash(sources), json.dumps(data, ensure_ascii=False), self.repo.clock()))
        return self.get(job["id"])

    def valid(self, job_id, sources):
        snapshot = self.get(job_id)
        return bool(snapshot and snapshot["source_hash"] == self.source_hash(sources)
                    and all(self.graph.valid(self.graph.get(nid, snapshot["scope"]))
                            and self.graph.get(nid)["digest"] == fingerprint
                            for nid, fingerprint in snapshot["data"]["node_versions"].items()))

    def apply(self, job_id, target_id):
        snapshot = self.get(job_id)
        if not snapshot or snapshot["applied"]:
            return []
        protected = set(snapshot["data"]["protected"])
        for nid in snapshot["data"]["node_ids"]:
            node = self.graph.get(nid, snapshot["scope"])
            if not node or not self.graph.valid(node):
                raise ValueError("巩固来源已失效")
            hot = nid in protected if self.settings.retention_enabled else node["source_type"] != "raw"
            self.conn.execute("""UPDATE association_nodes SET consolidated_into=?,retrieval_status=?,
                activation_factor=?,retention_reason=? WHERE id=?""",
                (target_id, "hot" if hot else "cold", 1.0 if hot else self.settings.cold_activation_factor,
                 f"{POLICY_VERSION}:job:{job_id}:{'protected' if hot else 'cold'}", nid))
        self.conn.execute("UPDATE consolidation_snapshots SET applied=1 WHERE job_id=?", (job_id,))
        return snapshot["data"]["protected"]

    def direct(self, row, source_type):
        node = (self.graph.for_memory(row["id"]) if source_type == "memory"
                else self.graph.for_message(row["id"]))
        if not node:
            return None
        if not self.graph.valid(node):
            return False
        return node["retrieval_status"] == "hot"

    def enforce_budget(self, scope=None, exempt=(), eligible=()):
        if not self.settings.retention_enabled:
            return 0
        exempt = set(exempt)
        eligible = set(eligible)
        for row in self.conn.execute("""SELECT s.data FROM consolidation_snapshots s JOIN jobs j ON j.id=s.job_id
            WHERE j.status IN ('pending','running')"""):
            exempt.update(json.loads(row[0])["node_ids"])
        sql = "SELECT * FROM association_nodes WHERE superseded=0 AND consolidated_into IS NOT NULL"
        rows = self.conn.execute(sql + (" AND scope=?" if scope else ""), (scope,) if scope else ()).fetchall()
        groups = {}
        for row in rows:
            node = self.graph.get(row["id"])
            if node["id"] in exempt or not self.graph.valid(node):
                continue
            if node["retrieval_status"] != "hot" and node["id"] not in eligible:
                continue  # A maintenance pass cannot undo every batch's TopK.
            kind = node["source_type"]
            if kind in {"raw", "episode"}:
                groups.setdefault((node["scope"], kind), []).append(node)
        changed = 0
        for (_, kind), nodes in groups.items():
            budget = self.settings.raw_hot_budget if kind == "raw" else self.settings.episode_hot_budget
            nodes.sort(key=lambda n: (-self.graph.strength(n), n["seq"], n["id"]))
            for rank, node in enumerate(nodes):
                status = "hot" if rank < budget else "cold"
                if node["retrieval_status"] == status:
                    continue
                self.conn.execute("""UPDATE association_nodes SET retrieval_status=?,activation_factor=?,
                    retention_reason=? WHERE id=?""", (status, 1 if status == "hot" else
                    self.settings.cold_activation_factor, f"{POLICY_VERSION}:budget", node["id"]))
                changed += 1
        return changed
