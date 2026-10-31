"""Versioned fact/reason corrections and rebuild jobs; original messages stay intact."""
from __future__ import annotations

import json

from .associations import digest

JOB_REPAIR = "repair_memory"


def validate_corrected_text(text, constraints):
    for correction in constraints:
        if correction["quote"] in text and correction["replacement"] not in text:
            raise ValueError("生成内容仍沿用已纠正的事实")


class CorrectionStore:
    def __init__(self, service):
        self.service, self.repo = service, service.repo
        self.conn, self.graph = self.repo.conn, service.graph
        self.conn.execute("""CREATE TABLE IF NOT EXISTS memory_corrections (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, operation_id TEXT NOT NULL,
            request_hash TEXT NOT NULL, node_id TEXT NOT NULL, chain_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL, quote TEXT NOT NULL, replacement TEXT NOT NULL, explanation TEXT NOT NULL,
            before_data TEXT NOT NULL, after_data TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(scope,operation_id))""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS correction_source ON memory_corrections(scope,node_id,seq)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS correction_chain ON memory_corrections(scope,chain_id,seq)")

    def waterline(self, scope):
        return self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM memory_corrections WHERE scope=?",
                                 (scope,)).fetchone()[0]

    def history(self, node_id, scope):
        return [{**dict(r), "before_data": json.loads(r["before_data"]), "after_data": json.loads(r["after_data"])}
                for r in self.conn.execute(
                    "SELECT * FROM memory_corrections WHERE scope=? AND node_id=? ORDER BY seq",
                    (scope, node_id))]

    def facts(self, node_id, scope):
        return [{"quote": r["quote"], "replacement": r["replacement"], "explanation": r["explanation"],
                 "operation_id": r["operation_id"]}
                for r in self.conn.execute("""SELECT * FROM memory_corrections
                    WHERE scope=? AND node_id=? AND kind='fact' ORDER BY seq""", (scope, node_id))]

    def reason(self, scope, chain_id):
        row = self.conn.execute("""SELECT * FROM memory_corrections WHERE scope=? AND chain_id=?
            AND kind IN ('reason','reject_reason') ORDER BY seq DESC LIMIT 1""", (scope, chain_id)).fetchone()
        return dict(row) if row else None

    def inherited_facts(self, node):
        """Corrections follow actual source dependencies, never shared topics."""
        roots = set(node["family"])
        rows = self.conn.execute("""SELECT c.*,n.family FROM memory_corrections c
            JOIN association_nodes n ON n.id=c.node_id WHERE c.scope=? AND c.kind='fact' ORDER BY c.seq""",
            (node["scope"],))
        return [{"quote": r["quote"], "replacement": r["replacement"], "explanation": r["explanation"],
                 "operation_id": r["operation_id"]}
                for r in rows if r["node_id"] == node["id"] or roots.intersection(json.loads(r["family"]))]

    def apply(self, *, scope, node_id, operation_id, expected_digest, kind, quote="",
              replacement="", explanation="", chain_id="", expected_chain_revision=None, context_id=""):
        if not operation_id or len(operation_id) > 160 or kind not in {"fact", "reason", "reject_reason"}:
            raise ValueError("纠错需要唯一操作编号和有效类型")
        if (not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 2000
                or not isinstance(replacement, str) or len(replacement) > 4000
                or not isinstance(quote, str) or len(quote) > 4000):
            raise ValueError("纠错需要明确说明，文本不得超过预算")
        request = dict(scope=scope, node_id=node_id, expected_digest=expected_digest, kind=kind, quote=quote,
                       replacement=replacement, explanation=explanation, chain_id=chain_id,
                       expected_chain_revision=expected_chain_revision, context_id=context_id)
        fingerprint = digest(request)
        old = self.conn.execute("SELECT * FROM memory_corrections WHERE scope=? AND operation_id=?",
                                (scope, operation_id)).fetchone()
        if old:
            if old["request_hash"] != fingerprint:
                raise ValueError("纠错操作编号已被不同请求使用")
            return {**json.loads(old["after_data"]), "status": "duplicate"}
        node = self.graph.get(node_id, scope)
        if not self.graph.valid(node) or node["digest"] != expected_digest:
            raise ValueError("纠错来源已变更，请重新查看当前记忆")
        material = self.graph.material(node)
        before = {"node": node, "content": material["text"]}
        repair_ids = []
        if kind == "fact":
            if not quote.strip() or quote not in material["text"] or not replacement.strip() or quote == replacement:
                raise ValueError("事实纠错必须提供来源中存在的片段和新事实")
            if node["memory_id"]:
                memory = self.repo.get_memory(node["memory_id"])
                if quote not in memory["content"]:
                    raise ValueError("请纠正该记忆正文中的事实，来源原话需在原始节点纠正")
                before["memory"] = memory
                self.repo.update_memory(memory["id"], content=memory["content"].replace(quote, replacement))
            repair_ids = self._invalidate_dependents(node, quote, operation_id)
        else:
            chain = self.conn.execute("""SELECT c.* FROM association_chains c JOIN chain_memberships m
                ON m.chain_id=c.id WHERE c.id=? AND c.scope=? AND m.node_id=?""",
                (chain_id, scope, node_id)).fetchone()
            if not chain or expected_chain_revision != chain["revision"]:
                raise ValueError("关联理由已变更，请重新查看")
            if kind == "reason" and not replacement.strip():
                raise ValueError("理由纠正不能为空")
            before["chain"] = dict(chain)
            self.conn.execute("UPDATE association_edges SET confidence=0 WHERE chain_id=?", (chain_id,))
            self.conn.execute("UPDATE association_chains SET reason_text=?,revision=revision+1 WHERE id=?",
                              (replacement if kind == "reason" else chain["reason_text"], chain_id))
            from .evidence import _tokens
            self.conn.execute("DELETE FROM association_reason_fts WHERE id=?", (chain_id,))
            if kind == "reason":
                self.conn.execute("INSERT INTO association_reason_fts VALUES(?,?,?)",
                                  (chain_id, scope, " ".join(_tokens(replacement))))
        result = {"status": "applied", "operation_id": operation_id, "node_id": node_id,
                  "repair_ids": repair_ids, "kind": kind}
        self.conn.execute("""INSERT INTO memory_corrections
            (scope,operation_id,request_hash,node_id,chain_id,kind,quote,replacement,explanation,
             before_data,after_data,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scope, operation_id, fingerprint, node_id, chain_id, kind, quote, replacement,
             explanation, json.dumps(before, ensure_ascii=False), json.dumps(result), self.repo.clock()))
        if kind != "fact":
            self.service.competition.reject(scope, chain_id, context_id)
        affected = {node_id}
        if chain_id:
            affected.update(r[0] for r in self.conn.execute(
                "SELECT node_id FROM chain_memberships WHERE chain_id=?", (chain_id,)))
        for nid in affected:
            current = self.graph.get(nid, scope)
            if kind == "fact":
                self.conn.execute("""UPDATE association_edges SET confidence=0
                    WHERE source_node=? OR target_node=?""", (nid, nid))
                current = (self.graph.register_memory(current["memory_id"]) if current["memory_id"]
                           else self.graph.register_raw([self.repo.get_message(i) for i in current["message_ids"]],
                                                        current["source_ref"]))
            self.graph.invalidate(nid)
            if current:
                self.conn.execute("UPDATE association_nodes SET ready=0 WHERE id=?", (nid,))
                existing = self.conn.execute("""SELECT 1 FROM jobs WHERE kind='associate'
                    AND status IN ('pending','running') AND json_extract(payload,'$.node_id')=?
                    AND json_extract(payload,'$.revision')=?""", (nid, current["revision"])).fetchone()
                if not existing:
                    self.repo.enqueue_job("associate", scope,
                        {"node_id": nid, "revision": current["revision"]},
                        f"correction:{operation_id}:{nid}")
        self.conn.execute("INSERT OR IGNORE INTO association_events(event_key,scope) VALUES(?,?)",
                          (f"correction:{scope}:{operation_id}", scope))
        return result

    def _invalidate_dependents(self, node, quote, operation_id):
        """Traverse real dependencies, skipping independent unrelated material."""
        sources = [("message", mid) for mid in node["message_ids"]]
        if node["memory_id"]:
            sources.append(("memory", node["memory_id"]))
        invalid = {mid for mid in self.service.dependencies.descendants(node["scope"], sources)
                   if not self.repo.get_memory(mid)["meta"].get("audit_only")}
        for mid in sorted(invalid):
            memory = self.repo.get_memory(mid)
            self.repo.update_memory(mid, meta={**memory["meta"], "repair_pending": operation_id})
            other = self.graph.for_memory(mid)
            if other:
                self.graph.invalidate(other["id"])
                self.conn.execute("""UPDATE association_edges SET confidence=0
                    WHERE source_node=? OR target_node=?""", (other["id"], other["id"]))
            self.repo.enqueue_job(JOB_REPAIR, node["scope"], {"memory_id": mid, "operation_id": operation_id},
                                  f"repair:{operation_id}:{mid}")
        return sorted(invalid)

    def repair_materials(self, job):
        memory = self.repo.get_memory(job["payload"]["memory_id"])
        if not memory or memory["deleted"] or not memory["meta"].get("repair_pending"):
            return None
        if memory["meta"]["repair_pending"] != job["payload"]["operation_id"]:
            return None
        from .dependencies import direct_dependencies
        dependencies = direct_dependencies(self.repo, memory)
        meta, sources, versions = memory["meta"], [], {}
        ids = {sid for kind, sid in dependencies if kind == "memory"}
        for mid in sorted(ids):
            source = self.repo.get_memory(mid)
            if not source or source["scope"] != job["scope"] or source["deleted"]:
                continue
            if source["meta"].get("repair_pending"):
                return False
            sources.append({"id": mid, "content": source["content"][:12000]})
            versions["memory:" + mid] = digest([source["content"], source["meta"]])
        message_ids = {int(sid) for kind, sid in dependencies if kind == "message"}
        for mid in sorted(message_ids):
            source = self.repo.get_message(mid)
            if source and source["scope"] == job["scope"] and self.graph._message_valid(mid, job["scope"]):
                raw = self.graph.for_message(mid)
                corrections = self.facts(raw["id"], job["scope"]) if raw else []
                sources.append({"id": f"message:{mid}", "content": source["content"][:12000],
                                "corrections": corrections})
                versions[f"message:{mid}"] = digest([source["content"], corrections])
        if not sources:
            self.repo.update_memory(memory["id"], meta={**memory["meta"],
                "repair_unavailable": "原始来源已不存在；保留旧文审计，暂停召回"})
            return None
        payload = {**job["payload"], "source_versions": versions,
                   "correction_waterline": self.waterline(job["scope"]),
                   "target_digest": digest([memory["content"], memory["meta"]])}
        self.repo.update_job_payload(job["id"], payload)
        job["payload"] = payload
        target = self.graph.for_memory(memory["id"])
        constraints = self.inherited_facts(target) if target else []
        # The triggering correction remains relevant even when an older derived
        # node has incomplete family metadata or rephrased the obsolete fact.
        trigger = self.conn.execute("""SELECT quote,replacement,explanation,operation_id
            FROM memory_corrections WHERE scope=? AND operation_id=? AND kind='fact'""",
            (job["scope"], job["payload"]["operation_id"])).fetchone()
        if trigger and not any(c["operation_id"] == trigger["operation_id"] for c in constraints):
            constraints.append(dict(trigger))
        return {"scope": job["scope"], "kind": memory["kind"], "sources": sources,
                "corrections": constraints}

    def complete_repair(self, job, content):
        memory = self.repo.get_memory(job["payload"]["memory_id"])
        payload = dict(job["payload"])
        materials = self.repair_materials(job)
        if materials is None:
            self.repo.finish_job(job["id"])
            return {"status": "ignored"}
        current = job["payload"]
        unchanged = (materials and all(current.get(key) == payload.get(key)
                     for key in ("source_versions", "correction_waterline", "target_digest")))
        if not unchanged:
            self.conn.execute("UPDATE jobs SET status='pending',lease_until=NULL WHERE id=?", (job["id"],))
            return {"status": "stale"}
        if not isinstance(content, str) or not content.strip() or len(content) > 20000:
            raise ValueError("重评后的记忆正文无效")
        for item in materials["corrections"]:
            if item["quote"] in content and item["replacement"] not in content:
                raise ValueError("重评仍保留已纠正的事实")
        history = list(memory["meta"].get("repair_history") or [])
        history.append({"operation_id": payload["operation_id"], "content": memory["content"]})
        meta = {**memory["meta"], "repair_history": history}
        meta.pop("repair_pending", None)
        self.repo.update_memory(memory["id"], content=content.strip(), meta=meta)
        self.graph.register_memory(memory["id"])
        self.repo.finish_job(job["id"])
        return {"status": "done", "memory_id": memory["id"]}
