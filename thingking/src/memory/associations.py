"""Scoped association sidecar; source text and semantic links remain separate."""
from __future__ import annotations

import hashlib
import json
import math

from .evidence import _active, _short_evidence, _tokens
from .lexical_index import query_terms
from .read_cache import read_cache

JOB_ASSOCIATE = "associate"
RELATIONS = {"support", "similar", "event", "cause", "contradicts"}
_SUPPORT = {"support": 1.0, "cause": .8, "event": .6, "similar": .3, "contradicts": 0.0}
WEIGHT_POLICY = "scoped-usage-v2"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS association_nodes (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
        scope TEXT NOT NULL, source_type TEXT NOT NULL, source_ref TEXT NOT NULL,
        memory_id TEXT, message_ids TEXT NOT NULL, family TEXT NOT NULL,
        digest TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        prior REAL NOT NULL DEFAULT .5, ready INTEGER NOT NULL DEFAULT 0,
        retrieval_status TEXT NOT NULL DEFAULT 'hot', consolidated_into TEXT,
        retention_reason TEXT, activation_factor REAL NOT NULL DEFAULT 1,
        superseded INTEGER NOT NULL DEFAULT 0, waiting_until REAL NOT NULL DEFAULT 0,
        UNIQUE(scope,source_type,source_ref))""",
    """CREATE TABLE IF NOT EXISTS association_message_nodes (
        message_id INTEGER PRIMARY KEY, node_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS association_chains (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, reason_key TEXT NOT NULL,
        reason_text TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE(scope,reason_key))""",
    """CREATE TABLE IF NOT EXISTS association_edges (
        id TEXT PRIMARY KEY, chain_id TEXT NOT NULL, source_node TEXT NOT NULL,
        target_node TEXT NOT NULL, relation_type TEXT NOT NULL, confidence REAL NOT NULL,
        evidence TEXT NOT NULL, source_revision INTEGER NOT NULL, target_revision INTEGER NOT NULL,
        direction TEXT NOT NULL DEFAULT 'bidirectional',
        UNIQUE(chain_id,source_node,target_node,relation_type))""",
    """CREATE TABLE IF NOT EXISTS chain_memberships (
        chain_id TEXT NOT NULL, node_id TEXT NOT NULL, recall_strength REAL NOT NULL,
        citation_strength REAL NOT NULL, scored_revision INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chain_id,node_id))""",
    "CREATE INDEX IF NOT EXISTS association_scope_nodes ON association_nodes(scope,source_type,seq)",
    "CREATE INDEX IF NOT EXISTS association_memory_nodes ON association_nodes(memory_id)",
    "CREATE INDEX IF NOT EXISTS association_member_nodes ON chain_memberships(node_id,chain_id)",
    "CREATE INDEX IF NOT EXISTS association_scope_chains ON association_chains(scope,id)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS association_reason_fts
        USING fts5(id UNINDEXED,scope UNINDEXED,tokens)""",
    "CREATE INDEX IF NOT EXISTS association_edges_source ON association_edges(source_node,chain_id)",
    "CREATE INDEX IF NOT EXISTS association_edges_target ON association_edges(target_node,chain_id)",
    """CREATE TABLE IF NOT EXISTS association_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL, scope TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS association_backfill (
        source_type TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(source_type,source_id))""",
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def bounded(value, low=0.0, high=1.0):
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("关联数值超出有效范围")
    return value


def _sigmoid(value):
    return 1 / (1 + math.exp(-value))


class AssociationStore:
    def __init__(self, repo, settings):
        self.repo, self.conn, self.settings = repo, repo.conn, settings
        self.reads = read_cache(repo)
        with repo.transaction():
            for statement in _SCHEMA:
                self.conn.execute(statement)
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES('association_schema_version','1')")
            policy = self.conn.execute("SELECT value FROM meta WHERE key='association_weight_policy'").fetchone()
            if not policy or policy[0] != WEIGHT_POLICY:
                self.conn.execute("UPDATE chain_memberships SET scored_revision=0")
                self.conn.execute("""INSERT INTO meta VALUES('association_weight_policy',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (WEIGHT_POLICY,))

    def get(self, node_id, scope=None):
        return self.reads.get("association_node", (node_id, scope), lambda: self._get(node_id, scope))

    def _get(self, node_id, scope):
        sql = "SELECT * FROM association_nodes WHERE id=?"
        row = self.conn.execute(sql + (" AND scope=?" if scope is not None else ""),
                                (node_id, scope) if scope is not None else (node_id,)).fetchone()
        if row:
            node = dict(row)
            for key in ("message_ids", "family"):
                node[key] = json.loads(node[key])
            return node

    def for_message(self, message_id):
        return self.reads.get("association_message", message_id, lambda: self._for_message(message_id))

    def _for_message(self, message_id):
        row = self.conn.execute("SELECT node_id FROM association_message_nodes WHERE message_id=?",
                                (message_id,)).fetchone()
        return self.get(row[0]) if row else None

    def for_memory(self, memory_id):
        return self.reads.get("association_memory", memory_id, lambda: self._for_memory(memory_id))

    def _for_memory(self, memory_id):
        row = self.conn.execute("SELECT id FROM association_nodes WHERE memory_id=?", (memory_id,)).fetchone()
        return self.get(row[0]) if row else None

    def material(self, node):
        if not node or node["superseded"]:
            return None
        key = (node["id"], node["scope"], tuple(node["message_ids"]), node.get("digest"))
        return self.reads.get("association_material", key, lambda: self._material(node))

    def _message(self, message_id):
        return self.reads.get("source_message", message_id, lambda: self.repo.get_message(message_id))

    def _memory(self, memory_id):
        return self.reads.get("source_memory", memory_id, lambda: self.repo.get_memory(memory_id))

    def _message_valid(self, message_id, scope):
        def valid():
            message = self._message(message_id)
            return bool(message and message["scope"] == scope
                        and _active(message, "message", include_archived=True))
        return self.reads.get("source_message_valid", (message_id, scope), valid)

    def _material(self, node):
        if node["source_type"] == "raw":
            messages = [self._message(i) for i in node["message_ids"]]
            if not messages or any(not self._message_valid(i, node["scope"]) for i in node["message_ids"]):
                return None
            return {"text": "\n".join(f"[{m['id']}][{m['role']}][{m['actor_id']}] {m['content']}"
                                     for m in messages), "sources": messages}
        memory = self._memory(node["memory_id"])
        if not memory or memory["scope"] != node["scope"] or not _active(memory, "memory"):
            return None
        dependencies = set(node.get("family") or [])
        dependencies.update(f"message:{mid}" for mid in memory["meta"].get("source_message_ids") or [])
        for root in dependencies:
            if root.startswith("message:"):
                if not self._message_valid(int(root.split(":", 1)[1]), node["scope"]):
                    return None
        text = memory["content"]
        if memory["meta"].get("experience_ref"):
            row = self.conn.execute("SELECT record FROM experiences WHERE source_ref=? AND scope=?",
                                     (memory["meta"]["experience_ref"], node["scope"])).fetchone()
            if not row:
                return None
            from .experience import text as experience_text
            record = json.loads(row[0])
            text += "\n" + experience_text(record.get("args", {})) + "\n" + experience_text(record.get("result", ""))
        return {"text": text, "sources": [memory]}

    def current_digest(self, node):
        if not node:
            return None
        key = (node["id"], node["scope"], tuple(node["message_ids"]), node.get("digest"))
        return self.reads.get("association_digest", key, lambda: self._current_digest(node))

    def _current_digest(self, node):
        material = self.material(node)
        if not material:
            return None
        return digest([[r["id"], r["scope"], r["content"], r.get("role"), r.get("actor_id"),
                        r.get("source_ref"), r.get("meta")] for r in material["sources"]])

    def valid(self, node):
        if not node:
            return False
        current = self.get(node["id"], node["scope"])
        matches_version = (current and not current["superseded"]
                           and current["revision"] == node["revision"] and current["digest"] == node["digest"])
        return bool(matches_version and self.current_digest(current) == node["digest"])

    def _roots(self, memory, seen=None):
        seen = set(seen or ())
        if memory["id"] in seen:
            return set()
        seen.add(memory["id"])
        meta, scope = memory["meta"], memory["scope"]
        if meta.get("experience_ref"):
            # A tool receipt is its own source; triggering user messages are dependencies.
            return {"experience:" + meta["experience_ref"]}
        roots = set()
        for mid in meta.get("source_message_ids") or []:
            message = self.repo.get_message(mid)
            if message and message["scope"] == scope:
                roots.add(f"message:{mid}")
        for ref in meta.get("source_message_refs") or []:
            row = self.conn.execute("SELECT id FROM messages WHERE source_ref=? AND scope=?", (ref, scope)).fetchone()
            if row:
                roots.add(f"message:{row[0]}")
        linked = [*(meta.get("source_memory_ids") or []), *(meta.get("episode_ids") or [])]
        if meta.get("episode_id"):
            linked.append(meta["episode_id"])
        for mid in linked:
            source = self.repo.get_memory(mid)
            if source and source["scope"] == scope:
                roots.update(self._roots(source, seen))
        if not roots:
            roots.update("experience:" + ref for ref in meta.get("source_experience_refs") or [])
        return roots or {f"memory:{memory['id']}"}

    def _upsert(self, scope, source_type, source_ref, memory_id, message_ids, family, prior=.5, background=False):
        node_id = "n:" + digest([scope, source_type, source_ref])[:32]
        old = self.get(node_id)
        prototype = dict(id=node_id, scope=scope, source_type=source_type, source_ref=source_ref,
                         memory_id=memory_id, message_ids=message_ids, superseded=0)
        fingerprint = self.current_digest(prototype)
        if not fingerprint:
            return None
        if old and not old["superseded"] and old["digest"] == fingerprint and old["family"] == sorted(family):
            return old
        revision = old["revision"] + 1 if old else 1
        consolidated = None
        status = "hot"
        if source_type == "raw":
            sources = [self.repo.get_message(i) for i in message_ids]
            consolidated = sources[0].get("episode_id") if all(m.get("episode_id") for m in sources) else None
            status = "cold" if consolidated else "hot"
        self.conn.execute("""INSERT INTO association_nodes
            (id,scope,source_type,source_ref,memory_id,message_ids,family,digest,revision,prior,
             retrieval_status,consolidated_into,activation_factor)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET digest=excluded.digest,revision=excluded.revision,
             message_ids=excluded.message_ids,family=excluded.family,prior=excluded.prior,
             ready=0,superseded=0,waiting_until=0""",
            (node_id, scope, source_type, source_ref, memory_id, json.dumps(message_ids),
             json.dumps(sorted(family)), fingerprint, revision, prior, status, consolidated,
             self.settings.cold_activation_factor if status == "cold" else 1.0))
        if old:
            for mid in set(old["message_ids"]) - set(message_ids):
                self.conn.execute("DELETE FROM association_message_nodes WHERE message_id=? AND node_id=?",
                                  (mid, node_id))
        for mid in message_ids:
            previous = self.for_message(mid)
            if previous and previous["id"] != node_id:
                self.conn.execute("UPDATE association_nodes SET superseded=1 WHERE id=?", (previous["id"],))
                self.invalidate(previous["id"])
                for residual in set(previous["message_ids"]) - set(message_ids):
                    self.conn.execute("DELETE FROM association_message_nodes WHERE message_id=? AND node_id=?",
                                      (residual, previous["id"]))
            self.conn.execute("""INSERT INTO association_message_nodes VALUES(?,?)
                ON CONFLICT(message_id) DO UPDATE SET node_id=excluded.node_id""", (mid, node_id))
        self.invalidate(node_id)
        self.conn.execute("INSERT OR IGNORE INTO association_events(event_key,scope) VALUES(?,?)",
                          (f"source:{node_id}:{revision}", scope))
        if self.settings.associations_enabled:
            self.repo.enqueue_job(JOB_ASSOCIATE, scope, {"node_id": node_id, "revision": revision,
                                                       "background": background},
                                  f"associate:{node_id}:{revision}")
        else:
            self.conn.execute("UPDATE association_nodes SET ready=1 WHERE id=?", (node_id,))
        return self.get(node_id)

    def register_raw(self, messages, source_ref=None, background=False):
        if not messages:
            return None
        scopes = {m["scope"] for m in messages}
        if len(scopes) != 1:
            raise ValueError("原始回合不可跨作用域")
        # A turn straddling the memory-day boundary belongs to its first message.
        # Timestamps stay intact; only pending consolidation grouping changes.
        first_day = min(messages, key=lambda m: (m["ts"], m["id"]))["day_key"]
        if all(m.get("episode_id") is None for m in messages):
            for message in messages:
                if message["day_key"] != first_day:
                    self.conn.execute("UPDATE messages SET day_key=? WHERE id=?", (first_day, message["id"]))
        ids = sorted({m["id"] for m in messages})
        return self._upsert(messages[0]["scope"], "raw", source_ref or f"message:{ids[0]}",
                            None, ids, {f"message:{i}" for i in ids}, background=background)

    def register_memory(self, memory_id, background=False):
        memory = self.repo.get_memory(memory_id)
        if not memory:
            return None
        node = self._upsert(memory["scope"], memory["kind"], f"memory:{memory_id}", memory_id,
                            [], self._roots(memory), .2 + min(10, max(0, memory["importance"])) * .06,
                            background=background)
        if node and memory["meta"].get("status") == "started":
            self.conn.execute("UPDATE association_nodes SET waiting_until=? WHERE id=?",
                              (self.repo.clock() + 2 * self.settings.job_lease_seconds, node["id"]))
            return self.get(node["id"])
        return node

    def backfill(self, limit=200):
        """Bound old-data work per maintenance pass; never infer adjacent turns."""
        count = 0
        rows = self.conn.execute("""SELECT m.* FROM memories m LEFT JOIN association_nodes n
            ON n.memory_id=m.id WHERE m.deleted=0 AND n.id IS NULL AND NOT EXISTS
            (SELECT 1 FROM association_backfill b WHERE b.source_type='memory' AND b.source_id=m.id)
            ORDER BY m.created_at DESC,m.id DESC LIMIT ?""",
                                 (limit,)).fetchall()
        for row in rows:
            count += bool(self.register_memory(row["id"], background=True))
            self.conn.execute("INSERT OR IGNORE INTO association_backfill VALUES('memory',?)", (row["id"],))
        rows = self.conn.execute("""SELECT m.* FROM messages m LEFT JOIN association_message_nodes n
            ON n.message_id=m.id WHERE n.message_id IS NULL AND NOT EXISTS
            (SELECT 1 FROM association_backfill b WHERE b.source_type='message' AND b.source_id=CAST(m.id AS TEXT))
            ORDER BY m.id DESC LIMIT ?""", (limit,)).fetchall()
        for row in rows:
            count += bool(self.register_raw([dict(row)], background=True))
            self.conn.execute("INSERT OR IGNORE INTO association_backfill VALUES('message',?)", (str(row["id"]),))
        return count

    def invalidate(self, node_id):
        self.conn.execute("""UPDATE association_chains SET revision=revision+1 WHERE id IN
            (SELECT chain_id FROM chain_memberships WHERE node_id=?)""", (node_id,))

    def waterline(self):
        return int(self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM association_events").fetchone()[0])

    def reasons(self, chain_ids, scope):
        ids = tuple(dict.fromkeys(chain_ids))
        if not ids:
            return []
        def load():
            rows = self.conn.execute(f"""SELECT id,reason_text FROM association_chains
                WHERE scope=? AND id IN ({','.join('?' for _ in ids)})""", (scope, *ids))
            found = {r["id"]: r["reason_text"] for r in rows}
            return [{"id": cid, "reason": found[cid]} for cid in ids if cid in found]
        return self.reads.get("association_reasons", (scope, ids), load)

    def weights(self, chain_id, scope):
        return self.reads.get("association_weights", (chain_id, scope), lambda: self._weights(chain_id, scope))

    def _weights(self, chain_id, scope):
        chain = self.conn.execute("SELECT * FROM association_chains WHERE id=? AND scope=?",
                                  (chain_id, scope)).fetchone()
        if not chain:
            return {}
        members = self.conn.execute("SELECT * FROM chain_memberships WHERE chain_id=?", (chain_id,)).fetchall()
        if any(m["scored_revision"] != chain["revision"] for m in members):
            nodes = {m["node_id"]: self.get(m["node_id"], scope) for m in members}
            nodes = {nid: n for nid, n in nodes.items() if self.valid(n)}
            supports = {nid: {} for nid in nodes}
            for edge in self.conn.execute("SELECT * FROM association_edges WHERE chain_id=?", (chain_id,)):
                a, b = nodes.get(edge["source_node"]), nodes.get(edge["target_node"])
                if not a or not b or a["revision"] != edge["source_revision"] or b["revision"] != edge["target_revision"]:
                    continue
                if set(a["family"]) & set(b["family"]):
                    continue
                contribution = edge["confidence"] * _SUPPORT[edge["relation_type"]]
                for source, target in ((a, b), (b, a)):
                    # Multiple representations/edges of one source give at most one contribution.
                    family = tuple(target["family"])
                    supports[source["id"]][family] = max(supports[source["id"]].get(family, 0), contribution)
            for nid, node in nodes.items():
                independent, seen = [], set()
                for family, contribution in sorted(supports[nid].items(), key=lambda item: (-item[1], item[0])):
                    if seen.intersection(family):
                        continue
                    seen.update(family)
                    independent.append(contribution)
                support = min(1.5, math.log1p(sum(independent[:3])))
                prior = min(.95, max(.05, node["prior"]))
                base = math.log(prior / (1 - prior)) + support
                uses = getattr(self.repo, "usage", None)
                counts = uses.counts(nid, chain_id) if uses else {}
                recall = min(.6, .1 * math.log1p(counts.get("presented", 0)))
                citation = min(.9, .25 * math.log1p(counts.get("cited", 0)))
                correction = min(2, .5 * counts.get("corrected", 0))
                self.conn.execute("""UPDATE chain_memberships SET recall_strength=?,citation_strength=?,
                    scored_revision=? WHERE chain_id=? AND node_id=?""",
                    (_sigmoid(base + recall + citation - correction),
                     _sigmoid(base + citation - correction), chain["revision"], chain_id, nid))
            self.conn.execute("UPDATE chain_memberships SET scored_revision=? WHERE chain_id=?",
                              (chain["revision"], chain_id))
        return {m["node_id"]: dict(m) for m in self.conn.execute(
            "SELECT * FROM chain_memberships WHERE chain_id=?", (chain_id,))
                if self.valid(self.get(m["node_id"], scope))}

    def _reason_quality(self, chain_id, scope, members):
        def load():
            quality = {}
            for edge in self.conn.execute("SELECT * FROM association_edges WHERE chain_id=?", (chain_id,)):
                if edge["source_node"] not in members or edge["target_node"] not in members:
                    continue
                a, b = self.get(edge["source_node"], scope), self.get(edge["target_node"], scope)
                current = (a and b and a["revision"] == edge["source_revision"]
                           and b["revision"] == edge["target_revision"])
                if not current or edge["confidence"] <= 0:
                    continue
                for nid in (a["id"], b["id"]):
                    quality[nid] = max(quality.get(nid, 0), edge["confidence"])
            return quality
        return self.reads.get("association_reason_quality", (chain_id, scope), load)

    def strength(self, node):
        return self.reads.get("association_strength", (node["id"], node["scope"]), lambda: self._strength(node))

    def _strength(self, node):
        scores = []
        for row in self.conn.execute("SELECT chain_id FROM chain_memberships WHERE node_id=?", (node["id"],)):
            members = self.weights(row[0], node["scope"])
            weight = members.get(node["id"])
            quality = self._reason_quality(row[0], node["scope"], members)
            if weight and quality.get(node["id"], 0) > 0:
                scores.append(weight["recall_strength"])
        uses = getattr(self.repo, "usage", None)
        if scores:
            return max(scores)
        return uses.prior(node) if uses else node["prior"]

    def submit(self, job, links, prior):
        payload, scope = job["payload"], job["scope"]
        source = self.get(payload["node_id"], scope)
        if not self.valid(source) or source["revision"] != payload["revision"]:
            return False
        expected = payload.get("candidate_versions") or {}
        if any(not self.valid(self.get(nid, scope)) or self.get(nid, scope)["digest"] != fingerprint
               for nid, fingerprint in expected.items()):
            return False
        if not isinstance(links, list) or len(links) > self.settings.association_candidates:
            raise ValueError("关联列表超过候选预算")
        prior = bounded(prior, .05, .95)
        validated = []
        for link in links:
            target = self.get(link.get("target_id"), scope)
            if not target or target["id"] == source["id"] or target["id"] not in expected:
                raise ValueError("关联目标不在授权材料中")
            reason = str(link.get("reason") or "").strip()
            reason_key = str(link.get("reason_key") or "").strip()
            relation = link.get("relation_type")
            direction = link.get("direction") or ("source_to_target" if relation == "cause" else "bidirectional")
            if not reason or len(reason) > 600 or not reason_key or len(reason_key) > 160 or relation not in RELATIONS:
                raise ValueError("关联需要明确理由、稳定理由键和有效类型")
            if direction not in {"source_to_target", "target_to_source", "bidirectional"}:
                raise ValueError("关联传播方向无效")
            evidence = link.get("evidence") or []
            quotes = {e.get("node_id"): e.get("quote") for e in evidence if isinstance(e, dict)}
            for node in (source, target):
                quote = quotes.get(node["id"])
                if not isinstance(quote, str) or not quote.strip() or quote not in self.material(node)["text"]:
                    raise ValueError("关联依据必须是双方材料中的真实连续片段")
            validated.append((target, reason_key, reason, relation,
                              bounded(link.get("confidence")), evidence, direction))
        self.conn.execute("UPDATE association_nodes SET prior=?,ready=1 WHERE id=?", (prior, source["id"]))
        self.invalidate(source["id"])
        for target, reason_key, reason, relation, confidence, evidence, direction in validated:
            cid = "c:" + digest([scope, reason_key])[:32]
            # Source relationships are already represented by family; they cannot reinforce themselves.
            if set(source["family"]) & set(target["family"]):
                continue
            self.conn.execute("""INSERT INTO association_chains(id,scope,reason_key,reason_text) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET reason_text=excluded.reason_text,revision=revision+1""",
                (cid, scope, reason_key, reason))
            self.conn.execute("DELETE FROM association_reason_fts WHERE id=?", (cid,))
            self.conn.execute("INSERT INTO association_reason_fts(id,scope,tokens) VALUES(?,?,?)",
                              (cid, scope, " ".join(_tokens(reason))))
            for node in (source, target):
                self.conn.execute("INSERT OR IGNORE INTO chain_memberships VALUES(?,?,.5,.5,0)", (cid, node["id"]))
            eid = digest([cid, source["id"], target["id"], relation])
            self.conn.execute("""INSERT INTO association_edges VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET confidence=excluded.confidence,evidence=excluded.evidence,
                 source_revision=excluded.source_revision,target_revision=excluded.target_revision,
                 direction=excluded.direction""",
                (eid, cid, source["id"], target["id"], relation, confidence,
                 json.dumps(evidence, ensure_ascii=False), source["revision"], target["revision"], direction))
        self.conn.execute("INSERT OR IGNORE INTO association_events(event_key,scope) VALUES(?,?)",
                          (f"job:{job['id']}", scope))
        return True

    def view(self, node, keys, query=""):
        if not self.valid(node):
            return None
        node = self.get(node["id"], node["scope"])
        if node["memory_id"]:
            memory = self._memory(node["memory_id"])
            evidence = keys.source_evidence(memory["id"], [node["scope"]], query=query,
                                            limit=self.settings.evidence_limit)
            view = dict(memory, memory_id=memory["id"], message_id=None, evidence=evidence)
        else:
            messages = self.material(node)["sources"]
            evidence = [dict(source_type="message", source_id=m["id"],
                             source_ref=m["source_ref"] or f"message:{m['id']}", scope=node["scope"],
                             role=m["role"], actor_id=m["actor_id"], actor_name=m["actor_name"],
                             content=_short_evidence(m["content"], query), ts=m["ts"]) for m in messages]
            view = dict(id=f"message:{messages[0]['id']}", memory_id=None, message_id=messages[0]["id"],
                        scope=node["scope"], kind="message", conversation_id=messages[0]["conversation_id"],
                        day_key=messages[0]["day_key"], content=self.material(node)["text"],
                        start_ts=min(m["ts"] for m in messages), end_ts=max(m["ts"] for m in messages),
                        evidence=evidence[:self.settings.evidence_limit], meta={})
        if not view["evidence"]:
            return None
        return dict(view, node_id=node["id"], citation_id=node["id"], node_digest=node["digest"],
                    source_family=node["family"], retrieval_status=node["retrieval_status"])

    def expand(self, query, seeds, scopes, visible):
        terms, activations, chain_ids = query_terms(query), {}, {}
        frontier = {nid: 1 / (i + 1) for i, nid in enumerate(dict.fromkeys(seeds)) if visible(self.get(nid))}
        reason_tokens = list(dict.fromkeys(_tokens(" ".join(sorted(terms)))))[:128]
        if reason_tokens and scopes:
            match = " OR ".join('"' + t.replace('"', '""') + '"' for t in reason_tokens)
            reasons = self.conn.execute(f"""SELECT id,scope FROM association_reason_fts
                WHERE scope IN ({','.join('?' for _ in scopes)}) AND association_reason_fts MATCH ?
                ORDER BY bm25(association_reason_fts),id LIMIT ?""",
                (*scopes, match, self.settings.association_candidates)).fetchall()
            for rank, reason in enumerate(reasons, 1):
                members = self.weights(reason["id"], reason["scope"])
                quality = self._reason_quality(reason["id"], reason["scope"], members)
                admitted = 0
                for nid, member in sorted(members.items(), key=lambda item: (-item[1]["recall_strength"], item[0])):
                    node = self.get(nid, reason["scope"])
                    if not visible(node):
                        continue
                    value = member["recall_strength"] * node["activation_factor"] * quality.get(nid, 0) / (rank + 1)
                    if value <= 0:
                        continue
                    admitted += 1
                    activations[nid] = max(activations.get(nid, 0), value)
                    chain_ids.setdefault(nid, set()).add(reason["id"])
                    frontier[nid] = max(frontier.get(nid, 0), value)
                    if admitted == 10:
                        break
                if len(activations) >= self.settings.graph_candidate_limit:
                    break
        frontier = dict(sorted(frontier.items(), key=lambda item: (-item[1], item[0]))
                        [:self.settings.graph_candidate_limit])
        for _ in range(self.settings.graph_max_hops):
            next_frontier = {}
            for nid, activation in frontier.items():
                node = self.get(nid)
                if not node or node["scope"] not in scopes:
                    continue
                rows = self.conn.execute("""SELECT e.*,c.reason_text FROM association_edges e
                    JOIN association_chains c ON c.id=e.chain_id WHERE c.scope=?
                    AND (e.source_node=? OR e.target_node=?) ORDER BY e.confidence DESC,e.id LIMIT ?""",
                    (node["scope"], nid, nid, self.settings.graph_candidate_limit)).fetchall()
                neighbors = []
                for edge in rows:
                    if edge["confidence"] <= 0:
                        continue
                    forward = edge["source_node"] == nid
                    if ((edge["direction"] == "source_to_target" and not forward)
                            or (edge["direction"] == "target_to_source" and forward)):
                        continue
                    target_id = edge["target_node"] if edge["source_node"] == nid else edge["source_node"]
                    target = self.get(target_id, node["scope"])
                    if not visible(target) or not self.valid(target):
                        continue
                    a, b = self.get(edge["source_node"]), self.get(edge["target_node"])
                    if a["revision"] != edge["source_revision"] or b["revision"] != edge["target_revision"]:
                        continue
                    reason_match = sum(t in edge["reason_text"].lower() for t in terms) / max(1, len(terms))
                    quality = edge["confidence"] * (.25 + .75 * reason_match)
                    neighbors.append((target, edge, quality))
                total = max(1.0, sum(quality for _, _, quality in neighbors))
                for target, edge, quality in neighbors:
                    weight = self.weights(edge["chain_id"], node["scope"]).get(target["id"])
                    value = activation * quality / total * .6 * target["activation_factor"]
                    value *= weight["recall_strength"] if weight else target["prior"]
                    if value <= 0:
                        continue
                    # Contradictions are comparison candidates, never a support propagation path.
                    activations[target["id"]] = max(activations.get(target["id"], 0), value)
                    chain_ids.setdefault(target["id"], set()).add(edge["chain_id"])
                    if edge["relation_type"] != "contradicts":
                        next_frontier[target["id"]] = max(next_frontier.get(target["id"], 0), value)
            frontier = dict(sorted(next_frontier.items(), key=lambda x: (-x[1], x[0]))
                            [:self.settings.graph_candidate_limit])
        return [(nid, value, sorted(chain_ids[nid])) for nid, value in
                sorted(activations.items(), key=lambda x: (-x[1], x[0]))[:self.settings.graph_candidate_limit]]
