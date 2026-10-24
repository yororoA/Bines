"""Actual presentation and validated citation receipts, deduplicated by source."""
from __future__ import annotations

import json
import math

from .associations import digest


class UsageStore:
    def __init__(self, repo, graph):
        self.repo, self.conn, self.graph = repo, repo.conn, graph
        self.conn.execute("""CREATE TABLE IF NOT EXISTS memory_usage_events (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, turn_id TEXT NOT NULL, node_id TEXT NOT NULL,
            chain_id TEXT NOT NULL DEFAULT '', family TEXT NOT NULL, event_type TEXT NOT NULL,
            receipt TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(scope,turn_id,family,event_type))""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS memory_usage_node ON memory_usage_events(node_id,chain_id,event_type)")

    def counts(self, node_id, chain_id=None):
        chain_filter = " AND u.chain_id=?" if chain_id is not None else ""
        return {row["event_type"]: row["n"] for row in self.conn.execute(
            """SELECT u.event_type,COUNT(*) AS n FROM memory_usage_events u
            JOIN association_nodes n ON n.id=u.node_id
            WHERE u.node_id=?""" + chain_filter + """
            AND json_extract(u.receipt,'$.node_digest')=n.digest GROUP BY u.event_type""",
            (node_id, chain_id) if chain_id is not None else (node_id,))}

    def prior(self, node):
        counts = self.counts(node["id"])
        prior = min(.95, max(.05, node["prior"]))
        value = math.log(prior / (1 - prior))
        value += min(.6, .1 * math.log1p(counts.get("presented", 0)))
        value += min(.9, .25 * math.log1p(counts.get("cited", 0)))
        return 1 / (1 + math.exp(-value))

    def _add(self, scope, turn_id, node, event_type, receipt):
        family = self.graph.evidence_family(node)
        # A summary and its raw messages are one source even if their root sets differ.
        rows = self.conn.execute("""SELECT family FROM memory_usage_events
            WHERE scope=? AND turn_id=? AND event_type=?""", (scope, turn_id, event_type))
        provenance = getattr(self.repo, "provenance", None)
        if any(family.intersection(provenance.family(scope, json.loads(row[0])) if provenance
                                   else json.loads(row[0])) for row in rows):
            return False
        cid = receipt.get("chain_id") or ""
        if cid and not self.conn.execute("SELECT 1 FROM chain_memberships WHERE node_id=? AND chain_id=?",
                                         (node["id"], cid)).fetchone():
            return False
        receipt = {**receipt, "node_digest": node["digest"]}
        key = digest([scope, turn_id, sorted(family), event_type])
        cursor = self.conn.execute("INSERT OR IGNORE INTO memory_usage_events VALUES(?,?,?,?,?,?,?,?,?)",
            (key, scope, turn_id, node["id"], cid, json.dumps(sorted(family)), event_type,
             json.dumps(receipt, ensure_ascii=False), self.repo.clock()))
        if cursor.rowcount:
            self.graph.invalidate(node["id"])
        return bool(cursor.rowcount)

    def record(self, scope, turn_id, presented, citations, answer):
        if not turn_id or not isinstance(presented, list) or not isinstance(citations, list):
            return {"presented": 0, "cited": 0}
        visible, result = {}, {"presented": 0, "cited": 0}
        for receipt in presented[:40]:
            if not isinstance(receipt, dict):
                continue
            node = self.graph.get(receipt.get("node_id"), scope)
            if not self.graph.valid(node) or node["digest"] != receipt.get("node_digest"):
                continue
            material = self.graph.material(node)
            originals = []
            for root in node["family"]:
                if root.startswith("message:"):
                    message = self.repo.get_message(int(root.split(":", 1)[1]))
                    if message and message["scope"] == scope:
                        originals.append(message["content"])
                elif root.startswith("experience:"):
                    row = self.conn.execute("SELECT record FROM experiences WHERE source_ref=? AND scope=?",
                                             (root[len("experience:"):], scope)).fetchone()
                    if row:
                        from .experience import text
                        originals.append(text(json.loads(row[0]).get("result", "")))
            if not originals and node["source_type"] == "raw":
                originals = [r["content"] for r in material["sources"]]
            if not originals and node["memory_id"]:
                originals = [material["text"]]
            corrections = getattr(self.repo, "corrections", None)
            facts = corrections.inherited_facts(node) if corrections else []
            originals.extend(fact["replacement"] for fact in facts)
            # Runtime sends only the spans actually included in a prompt/tool result.
            spans = receipt.get("spans") or []
            if not spans or any(not isinstance(s, str) or not s.strip()
                                or not any(s in text for text in [material["text"], *originals]) for s in spans):
                continue
            allowed_chains = set()
            chain_ids = receipt.get("chain_ids") or []
            chain_ids = chain_ids[:20] if isinstance(chain_ids, list) else []
            for cid in chain_ids:
                if not isinstance(cid, str):
                    continue
                if self.conn.execute("""SELECT 1 FROM chain_memberships m JOIN association_chains c ON c.id=m.chain_id
                    WHERE m.node_id=? AND c.id=? AND c.scope=?""", (node["id"], cid, scope)).fetchone():
                    allowed_chains.add(cid)
            attributed = next(iter(allowed_chains)) if len(allowed_chains) == 1 else ""
            visible[node["id"]] = (node, spans, originals, allowed_chains, attributed, receipt, facts)
            result["presented"] += self._add(scope, turn_id, node, "presented",
                                             {**receipt, "chain_id": attributed})
        for receipt in citations[:20]:
            if not isinstance(receipt, dict) or receipt.get("source_id") not in visible:
                continue
            node, spans, originals, allowed_chains, attributed, presentation, facts = visible[receipt["source_id"]]
            cid = receipt.get("chain_id") or attributed
            if cid and cid not in allowed_chains:
                continue
            answer_span = receipt.get("answer_span")
            quote = receipt.get("source_quote") or ""
            if not isinstance(answer_span, str) or not answer_span.strip() or answer_span not in str(answer):
                continue
            if quote and (not isinstance(quote, str) or not any(quote in span for span in spans)):
                continue
            if quote and not any(quote in original for original in originals):
                continue
            if quote and any(fact["quote"] in quote for fact in facts):
                continue
            validated = {**receipt, "chain_id": cid}
            added = self._add(scope, turn_id, node, "cited", validated)
            result["cited"] += added
            if added and getattr(self.repo, "competition", None):
                self.repo.competition.learn(scope, turn_id, node, presentation, validated)
        if result["presented"] or result["cited"]:
            self.conn.execute("INSERT OR IGNORE INTO association_events(event_key,scope) VALUES(?,?)",
                              (f"usage:{turn_id}", scope))
        return result
