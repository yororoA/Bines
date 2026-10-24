"""Local administration views. QQ identities cannot call these RPC methods."""
from __future__ import annotations

import json
from .evidence import _active


class MemoryAdmin:
    def __init__(self, service):
        self.service = service
        self.repo, self.graph = service.repo, service.graph

    def nodes(self, scopes=None, source_type="", status="", ready=None, keyword="", offset=0, limit=50):
        conditions, params = ["n.superseded=0"], []
        if scopes:
            conditions.append(f"n.scope IN ({','.join('?' for _ in scopes)})")
            params.extend(scopes)
        for column, value in (("source_type", source_type), ("retrieval_status", status)):
            if value:
                conditions.append(f"n.{column}=?")
                params.append(value)
        if ready is not None:
            conditions.append("n.ready=?")
            params.append(int(ready))
        if keyword:
            conditions.append("(COALESCE(m.content,'') LIKE ? OR EXISTS "
                              "(SELECT 1 FROM association_message_nodes a JOIN messages x ON x.id=a.message_id "
                              "WHERE a.node_id=n.id AND x.content LIKE ?))")
            params.extend([f"%{keyword}%"] * 2)
        # Do not expose deleted sources through the admin detail or list.
        conditions.append("(n.memory_id IS NULL OR m.deleted=0)")
        sql = (" FROM association_nodes n LEFT JOIN memories m ON m.id=n.memory_id "
               "LEFT JOIN messages first_message ON first_message.id=json_extract(n.message_ids,'$[0]') WHERE "
               + " AND ".join(conditions))
        total = self.repo.conn.execute("SELECT COUNT(*)" + sql, params).fetchone()[0]
        rows = self.repo.conn.execute("SELECT n.id" + sql
                                     + " ORDER BY COALESCE(m.end_ts,first_message.ts,m.created_at) DESC,n.seq DESC LIMIT ? OFFSET ?",
                                     (*params, max(1, min(100, int(limit))), max(0, int(offset))))
        items = []
        for row in rows.fetchall():
            node = self.graph.get(row["id"])
            material = self.graph.material(node)
            if not material:
                continue
            source = material["sources"][0]
            items.append({**node, "content": material["text"][:1200],
                          "day_key": source.get("day_key"), "conversation_id": source.get("conversation_id"),
                          "created_at": source.get("ts") or source.get("created_at"),
                          "chain_count": self.repo.conn.execute(
                              "SELECT COUNT(*) FROM chain_memberships WHERE node_id=?", (node["id"],)).fetchone()[0]})
        return {"items": items, "total": total}

    def detail(self, node_id=None, memory_id=None, scope=None, message_id=None):
        node = (self.graph.get(node_id, scope) if node_id else self.graph.for_memory(memory_id) if memory_id
                else self.graph.for_message(int(message_id)) if message_id is not None else None)
        registered = node is not None
        material = self.graph.material(node) if node else None
        if not node and memory_id:
            source = self.repo.get_memory(memory_id)
            from .graph_view import independent_material
            prototype, unlinked = independent_material(self.graph, source) if source else (None, None)
            if unlinked and (scope is None or scope == source["scope"]):
                node = {"id": None, "memory_id": memory_id, "scope": source["scope"],
                        "source_type": source["kind"], "source_ref": f"memory:{memory_id}",
                        "family": prototype["family"], "revision": 0, "ready": 0,
                        "retrieval_status": "hot", "retention_reason": None}
                material = unlinked
        if not node and message_id is not None:
            source = self.repo.get_message(int(message_id))
            if source and _active(source, "message", include_archived=True) and (scope is None or scope == source["scope"]):
                node = {"id": None, "memory_id": None, "scope": source["scope"],
                        "source_type": "raw", "source_ref": source["source_ref"] or f"message:{source['id']}",
                        "family": [f"message:{source['id']}"], "message_ids": [source["id"]],
                        "revision": 0, "ready": 0,
                        "retrieval_status": "cold" if source.get("episode_id") else "hot", "retention_reason": None}
                material = {"text": source["content"], "sources": [source]}
        if not node or scope is not None and node["scope"] != scope:
            return None
        if not material:
            return None
        memberships = []
        for row in self.repo.conn.execute(
            "SELECT c.* FROM chain_memberships m JOIN association_chains c ON c.id=m.chain_id "
            "WHERE m.node_id=? AND c.scope=? ORDER BY c.reason_key", (node["id"], node["scope"])
        ).fetchall():
            weight = self.graph.weights(row["id"], node["scope"]).get(node["id"])
            if weight:
                memberships.append({**dict(row), **weight,
                                    "usage": self.service.usage.counts(node["id"], row["id"])})
        edges = []
        for row in self.repo.conn.execute(
            "SELECT e.*,c.reason_text FROM association_edges e JOIN association_chains c ON c.id=e.chain_id "
            "WHERE c.scope=? AND (e.source_node=? OR e.target_node=?) ORDER BY e.confidence DESC LIMIT 100",
            (node["scope"], node["id"], node["id"])
        ).fetchall():
            other_id = row["target_node"] if row["source_node"] == node["id"] else row["source_node"]
            other = self.graph.get(other_id, node["scope"])
            if not self.graph.valid(other):
                continue
            current_revision = row["source_revision"] if row["source_node"] == node["id"] else row["target_revision"]
            other_revision = row["target_revision"] if row["source_node"] == node["id"] else row["source_revision"]
            if node["revision"] != current_revision or other["revision"] != other_revision:
                continue
            edges.append({**dict(row), "evidence": json.loads(row["evidence"]),
                          "other": {"id": other_id, "source_type": other["source_type"],
                                    "content": self.graph.material(other)["text"][:600],
                                    "retrieval_status": other["retrieval_status"]}})
        roots = []
        for root in node["family"]:
            kind, ref = root.split(":", 1)
            source = (self.repo.get_message(int(ref)) if kind == "message"
                      else self.repo.get_memory(ref) if kind == "memory" else None)
            if source and source["scope"] == node["scope"] and not source.get("deleted"):
                if kind == "message" and not self.graph._message_valid(source["id"], node["scope"]):
                    continue
                roots.append({"source_type": kind, **source})
            elif kind == "experience":
                row = self.repo.conn.execute(
                    "SELECT e.record FROM experiences e JOIN memories m ON m.id=e.memory_id "
                    "WHERE e.source_ref=? AND e.scope=? AND m.deleted=0", (ref, node["scope"])).fetchone()
                if row:
                    roots.append({"source_type": kind, "source_ref": ref, "record": json.loads(row[0])})
        snapshots = []
        for row in self.repo.conn.execute(
            "SELECT s.job_id FROM consolidation_snapshots s WHERE s.scope=? AND EXISTS "
            "(SELECT 1 FROM json_each(s.data,'$.node_ids') WHERE value=?) ORDER BY s.job_id DESC LIMIT 20",
            (node["scope"], node["id"])
        ):
            snapshots.append(self.service.retention.get(row[0]))
        jobs = [dict(row) for row in self.repo.conn.execute(
            "SELECT id,kind,status,attempts,last_error,created_at,updated_at FROM jobs "
            "WHERE scope=? AND json_extract(payload,'$.node_id')=? ORDER BY id DESC LIMIT 20",
            (node["scope"], node["id"]))]
        usage = [{**dict(row), "receipt": json.loads(row["receipt"])} for row in self.repo.conn.execute(
            "SELECT event_type,turn_id,chain_id,receipt,created_at FROM memory_usage_events "
            "WHERE scope=? AND node_id=? ORDER BY created_at DESC LIMIT 30", (node["scope"], node["id"]))]
        return {"node": node, "content": material["text"], "sources": material["sources"],
                "roots": roots, "memberships": memberships, "edges": edges, "snapshots": snapshots,
                "jobs": jobs, "usage": usage, "strength": self.graph.strength(node) if registered else None,
                "valid": self.graph.valid(node) if registered else True, "registered": registered}

    def jobs(self, offset=0, limit=30, status=""):
        clause, params = (" WHERE status=?", [status]) if status else ("", [])
        total = self.repo.conn.execute("SELECT COUNT(*) FROM jobs" + clause, params).fetchone()[0]
        items = [dict(row) for row in self.repo.conn.execute(
            "SELECT id,kind,scope,status,attempts,last_error,created_at,updated_at FROM jobs" + clause
            + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(100, int(limit))), max(0, int(offset))))]
        return {"items": items, "total": total}
