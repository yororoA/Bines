"""统一检索：全文与语义候选以 RRF 融合，重排后返回有来源的原文证据。"""
from __future__ import annotations

import logging
import hashlib
import math
import re
from datetime import datetime

from .evidence import EvidenceStore, _tokens
from .lexical_index import query_terms
from .models import MEMORY_KINDS, ZONE

log = logging.getLogger("bines.memory.retrieval")


def _source_time_visible(timestamp, when):
    # Zero is a valid epoch; missing/invalid source time cannot prove that the
    # original existed at the requested historical instant.
    return (isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)
            and math.isfinite(timestamp) and timestamp <= when)


def rrf(rankings, constant=60):
    """只比较排名，不把 BM25 分数与余弦距离当作同一量纲。"""
    scores, channels = {}, {}
    for channel, ids in rankings.items():
        for rank, key_id in enumerate(dict.fromkeys(ids), 1):
            scores[key_id] = scores.get(key_id, 0.0) + 1.0 / (constant + rank)
            channels.setdefault(key_id, {})[channel] = rank
    return scores, channels


def time_intent(query):
    """日期只用作软排序提示；明确 as_of 参数才进行事实有效期过滤。"""
    match = re.search(r"(20\d{2})[年/-](\d{1,2})(?:[月/-](\d{1,2}))?", query)
    when = None
    if match:
        try:
            when = datetime(int(match[1]), int(match[2]), int(match[3] or 1), tzinfo=ZONE).timestamp()
        except ValueError:
            pass
    history = any(word in query for word in ("以前", "曾经", "之前", "历史", "当时", "变化"))
    return when, history or when is not None


class HybridRetriever:
    def __init__(self, repo, index, settings):
        self.repo, self.index, self.settings = repo, index, settings
        self.keys = EvidenceStore(repo, index=index)
        self._vector_reconciled = False
        self.last_trace = {}

    def sync(self, vectors=False, rebuild=False, full=False):
        maintenance = getattr(self.repo, "vector_maintenance", None)
        if vectors and maintenance is not None:
            # Periodic vector reconciliation only schedules work. The worker's
            # bounded tick maintains keys; search(vectors=False) still flushes
            # lexical changes before querying. Explicit online rebuilds enter
            # through MemoryService.rebuild_index -> maintenance.rebuild.
            maintenance.request()
            return {**dict.fromkeys(("added", "updated", "deleted", "unchanged", "total", "fts_repaired", "vectors"), 0),
                    "vector_status": maintenance.status()}
        if rebuild:
            self.keys.clear_index_marks()
        result = self.keys.sync(rebuild=rebuild, repair_fts=rebuild)
        result["vectors"] = 0
        if vectors and hasattr(self.index, "upsert_keys"):
            try:
                prepare = getattr(self.index, "prepare", None)
                if prepare is not None and prepare(self.repo):
                    self._vector_reconciled = False
                batch_size = max(1, min(512, getattr(self.settings, "index_batch_size", 64)))
                if not self._vector_reconciled or rebuild:
                    # One startup repair handles indexes made before deletion
                    # tombstones existed. Normal batches never enumerate vectors.
                    alive = set(self.keys.all_ids())
                    self.repo.conn.executemany("INSERT OR IGNORE INTO retrieval_vector_deletions VALUES(?)",
                                               [(key,) for key in set(self.index.key_ids()) - alive])
                    self._vector_reconciled = not self.repo.conn.in_transaction
                while True:
                    obsolete = [r[0] for r in self.repo.conn.execute(
                        "SELECT id FROM retrieval_vector_deletions ORDER BY id LIMIT ?", (batch_size,))]
                    if not obsolete:
                        break
                    self.index.delete_keys(obsolete)
                    self.repo.conn.executemany("DELETE FROM retrieval_vector_deletions WHERE id=?", [(key,) for key in obsolete])
                    if not full:
                        break
                while batch := self.keys.unindexed_keys(limit=batch_size):
                    # A re-created key may still have a queued deletion. Cancel
                    # it only after a successful upsert, preserving failed work.
                    ids = self.index.upsert_keys(batch)
                    if not ids:
                        break
                    self.repo.conn.executemany("DELETE FROM retrieval_vector_deletions WHERE id=?", [(key,) for key in ids])
                    self.keys.mark_indexed(ids)
                    result["vectors"] += len(ids)
                    if not full:
                        break
                publish = getattr(self.index, "publish_if_ready", None)
                if publish is not None:
                    publish(self.repo)
            except Exception:
                log.exception("检索键向量更新失败，保留待重建标记，全文检索继续可用")
        return result

    def search(self, query, scopes, kinds=None, k=10, excluded=(), as_of=None, include_history=False,
               expand_graph=True, include_evidence=True, retrieval_mode="auto"):
        with self.repo.transaction(), self.keys.reads.session():
            return self._search(query, scopes, kinds, k, excluded, as_of, include_history, expand_graph, include_evidence, retrieval_mode)

    def _search(self, query, scopes, kinds, k, excluded, as_of, include_history, expand_graph, include_evidence, retrieval_mode="auto"):
        self.last_trace = {"candidates": 0, "vector": {"status": "disabled"}, "vector_attempts": [],
                           "retrieval_mode": retrieval_mode, "status": "empty_query"}
        if not isinstance(query, str) or not query.strip() or not scopes:
            return []
        k = max(1, min(int(k), 50))
        count = max(k, self.settings.retrieval_candidates)
        anchors = re.findall(r'[“"「]([^”"」]{2,})[”"」]', query)
        anchors += re.findall(r'(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*\d)[A-Za-z][A-Za-z0-9_-]{2,}(?![A-Za-z0-9_-])|(?<![A-Za-z0-9])\d{4,}(?![A-Za-z0-9])', query)
        exact = bool(anchors or re.search(r'原话|原文|编号|代号', query))
        if retrieval_mode not in {"auto", "exact", "association", "explore"}:
            raise ValueError("unknown retrieval_mode")
        mode = ("exact" if exact else "association") if retrieval_mode == "auto" else retrieval_mode
        if mode == "explore" and not getattr(self.settings, "retrieval_exploration_enabled", False):
            mode = "association"
        scopes = list(dict.fromkeys(scopes))
        self.sync()
        # 原始消息与派生记忆都可命中；限定 semantic 的画像查询不混入聊天正文。
        allowed_kinds = list(kinds) if kinds else [*MEMORY_KINDS, "message"]
        terms = query_terms(query)
        topic = " ".join(sorted(terms)) or query
        date, historical = time_intent(query)
        historical = include_history or historical
        excluded = set(excluded)
        when = self.repo.temporal._as_of(as_of)

        def eligible(keys):
            parent_ids = [row["meta"].get("episode_id") for row in keys.values()
                          if row["meta"].get("retrieval_status") == "cold" and row["meta"].get("episode_id")]
            visible = set(self.repo.temporal.visible_memory_ids(
                [row["memory_id"] for row in keys.values() if row["memory_id"]] + parent_ids,
                as_of=as_of, include_history=historical))
            return {kid: row for kid, row in keys.items()
                    if (row["memory_id"] or f"message:{row['message_id']}") not in excluded
                    and (not row["memory_id"] or row["memory_id"] in visible)
                    and (row["message_id"] is None or _source_time_visible(row["meta"].get("ts"), when))
                    and (row["meta"].get("retrieval_status") != "cold" or (
                        not row["meta"].get("episode_id") or row["meta"]["episode_id"] in visible))}

        # Scan ranked lexical batches until enough eligible independent sources
        # survive. Expired facts and a long source's chunks cannot spend the quota.
        lexical = self.keys.bm25_query(topic, scopes, allowed_kinds, count,
                                       accept=eligible, per_source=1)
        rankings = {"bm25": [key for key, _ in lexical]}
        vector_distances = {}
        vector_requested = 0
        vector_attempts = []
        if hasattr(self.index, "query_keys") or getattr(self.index, "persistent", True):
            try:
                requested, ceiling = count, min(2048, max(256, count * 16))
                while True:
                    if hasattr(self.index, "query_keys"):
                        vector = self.index.query_keys(query, scopes, allowed_kinds, requested)
                    else:
                        vector = [(f"m:{mid}:0", distance) for mid, distance in
                                  self.index.query(query, scopes, [x for x in allowed_kinds if x != "message"], n=requested)]
                    vector_attempts.append(dict(getattr(self.index, "last_query_trace", {}) or
                                                {"status": "ok" if vector else "empty"}))
                    vector_requested = requested
                    live = eligible(self.keys.get_keys([key for key, _ in vector], scopes))
                    hashes = getattr(vector, "source_hashes", None)
                    if hashes is not None:
                        live = {key: row for key, row in live.items() if hashes.get(key) == row["source_hash"]}
                    search_hashes = getattr(vector, "search_hashes", None)
                    if search_hashes is not None:
                        live = {key: row for key, row in live.items()
                                if search_hashes.get(key) == hashlib.sha256(row["search_text"].encode()).hexdigest()}
                    selected, sources = [], set()
                    for key, distance in vector:
                        row = live.get(key)
                        if not row:
                            continue
                        source = (row["source_type"], row["memory_id"] or row["message_id"])
                        if source in sources:
                            continue
                        sources.add(source)
                        selected.append((key, distance))
                        if len(selected) == count:
                            break
                    if len(selected) >= count or len(vector) < requested or requested >= ceiling:
                        vector_distances = dict(selected)
                        rankings["vector"] = [key for key, _ in selected]
                        break
                    requested = min(ceiling, requested * 2)
            except Exception:
                vector_attempts.append({"status": "error"})
                log.exception("向量检索失败，使用全文候选")
        scores, channels = rrf(rankings, self.settings.retrieval_rrf_constant)
        keys = eligible(self.keys.get_keys(list(scores), scopes))
        candidates = []
        for key_id, row in keys.items():
            content = (row["content"] + "\n" + row["meta"].get("lexical_context", row["search_text"])).lower()
            coverage = sum(term in content for term in terms) / max(1, len(terms))
            # 向量通道独立命中允许同义改写；全文通道至少需要一个主题命中。
            if not coverage and vector_distances.get(key_id, 1.0) > self.settings.retrieval_max_distance:
                continue
            # 默认透明、确定性的局部重排；可选 CrossEncoder 在有限候选内做语义重排。
            score = scores[key_id] + coverage / (self.settings.retrieval_rrf_constant + 1)
            if date is not None and datetime.fromtimestamp(date, ZONE).strftime("%Y-%m") in content:
                score += 0.002
            row = {**row, "rrf_score": scores[key_id], "score": score,
                   "retrieval_channels": channels[key_id]}
            candidates.append(row)
        candidates.sort(key=lambda row: (-row["score"], row["id"]))
        self.last_trace = {"candidates": len(candidates), "channels": {name: len(ids) for name, ids in rankings.items()},
                           "reranker": "local", "as_of": as_of, "historical": historical,
                           "vector_requested": vector_requested, "vector_attempts": vector_attempts,
                           "vector": vector_attempts[-1] if vector_attempts else {"status": "disabled"},
                           "retrieval_mode": mode, "cold_fallback": 0}
        if self.settings.reranker_model and candidates:
            candidates = self._rerank(query, candidates[:count])
        # Cold originals have only lexical keys. Rescue a few precise matches
        # without exposing all archived dialogue to normal vector retrieval.
        quota = max(0, min(10, getattr(self.settings, "cold_fallback_limit", 3)))
        if quota and "message" in allowed_kinds and (mode == "exact" or len(candidates) < k):
            cold = self.keys.bm25_query(" ".join(anchors) or topic, scopes, ["message"], quota * 4, accept=eligible,
                per_source=1, cold_only=True, scan_limit=128)
            cold_keys = eligible(self.keys.get_keys([kid for kid, _ in cold], scopes, include_cold=True))
            cold_count = 0
            for key_id, _ in cold:
                row = cold_keys.get(key_id)
                if not row:
                    continue
                coverage = sum(t in row["content"].lower() for t in terms) / max(1, len(terms))
                if anchors:
                    if not any(anchor.lower() in row["content"].lower() for anchor in anchors):
                        continue
                elif coverage < .75:
                    continue
                score = (1 + coverage) / (self.settings.retrieval_rrf_constant + 1)
                candidates.append({**row, "score": score, "rrf_score": score,
                                   "retrieval_channels": {"cold_fts": cold_count + 1}})
                cold_count += 1
                if cold_count == quota:
                    break
            self.last_trace["cold_fallback"] = cold_count
        if mode == "exact":
            candidates.sort(key=lambda row: (bool(anchors) and not any(
                anchor.lower() in row["content"].lower() for anchor in anchors), -row["score"], row["id"]))
        graph = getattr(self.repo, "associations", None)
        graph_enabled = graph is not None and self.settings.associations_enabled and expand_graph
        output, seen = [], set()
        for row in candidates:
            source_id = (row["source_type"], row["memory_id"] or row["message_id"])
            if source_id in seen:
                continue
            evidence = []
            if not graph_enabled and include_evidence:
                evidence = self.keys.evidence(row, scopes, query=query, limit=self.settings.evidence_limit,
                                              include_cold=row["meta"].get("retrieval_status") == "cold")
                if not evidence:
                    continue
            seen.add(source_id)
            memory = self.repo.get_memory(row["memory_id"]) if row["memory_id"] and not graph_enabled else None
            # key.content 是原文连续片段；evidence 指向原始轮次，不能把提取事实当成用户原话。
            output.append({
                **(memory or {}), "id": row["memory_id"] or f"message:{row['message_id']}",
                "memory_id": row["memory_id"], "message_id": row["message_id"],
                "key_id": row["id"], "scope": row["scope"], "kind": row["kind"],
                "content": memory["content"] if memory else row["content"], "evidence": evidence,
                "matched_spans": [row["content"]],
                "score": row["score"], "rrf_score": row["rrf_score"],
                "retrieval_channels": row["retrieval_channels"], "origin": row["meta"].get("origin"),
                "retrieval_status": row["meta"].get("retrieval_status", "hot"),
            })
            if len(output) == (count if graph_enabled else k):
                break
        if not graph_enabled:
            return self._describe(output[:k], as_of)
        return self._graph_fusion(query, output, scopes, allowed_kinds, k, excluded, as_of, historical, mode)

    def _graph_fusion(self, query, direct, scopes, kinds, k, excluded, as_of, historical, mode="association"):
        graph = self.repo.associations
        when = self.repo.temporal._as_of(as_of)

        def visible(node):
            if not node or node["scope"] not in scopes or not graph.valid(node):
                return False
            if node["source_type"] == "raw":
                if "message" not in kinds or any(f"message:{i}" in excluded for i in node["message_ids"]):
                    return False
                for mid in node["message_ids"]:
                    message = graph._message(mid)
                    if not message or not _source_time_visible(message.get("ts"), when):
                        return False
                    if node["retrieval_status"] == "cold":
                        if not self.keys._cold_source(message, "message"):
                            return False
                        if message.get("episode_id") and not self.repo.temporal.visible_memory_ids(
                                [message["episode_id"]], as_of=as_of, include_history=historical):
                            return False
                return True
            return (node["source_type"] in kinds and node["memory_id"] not in excluded
                    and bool(self.repo.temporal.visible_memory_ids(
                        [node["memory_id"]], as_of=as_of, include_history=historical)))

        rows, direct_ids = {}, []
        for hit in direct:
            node = (graph.for_memory(hit["memory_id"]) if hit["memory_id"]
                    else graph.for_message(hit["message_id"]))
            if not node:
                node = (graph.register_memory(hit["memory_id"]) if hit["memory_id"]
                        else graph.register_raw([self.repo.get_message(hit["message_id"])]))
            if not visible(node):
                continue
            if node["id"] not in rows:
                rows[node["id"]] = hit
                direct_ids.append(node["id"])
        expansion_enabled = getattr(self.settings, "graph_expansion_enabled", True)
        expanded = graph.expand(query, direct_ids, scopes, visible) if expansion_enabled else []
        graph_ids, exploration = [], []
        minimum = max(0.0, getattr(self.settings, "graph_min_activation", .001))
        for nid, activation, chains in expanded:
            if activation < minimum:
                if activation >= minimum / 10 and nid not in rows:
                    exploration.append((nid, activation, chains))
                continue
            rows[nid] = {**rows.get(nid, {}), "graph_activation": activation, "chain_ids": chains}
            graph_ids.append(nid)
        # Preserve absolute activation as well as rank. The .05 pivot is an
        # activation scale (not a probability); arbitrarily tiny edges cannot
        # gain a full RRF vote simply by being the sole graph candidate.
        constant = self.settings.retrieval_rrf_constant
        weight = max(0.0, min(.9, getattr(self.settings, "graph_fusion_weight", .5)))
        direct_weight = 1.0 if mode == "exact" else 1.0 - weight
        scores, channels = {}, {}
        for rank, nid in enumerate(direct_ids, 1):
            scores[nid] = direct_weight / (constant + rank)
            channels[nid] = {"direct": rank}
        for rank, nid in enumerate(graph_ids, 1):
            activation = rows[nid]["graph_activation"]
            calibrated = activation / (activation + .05)
            scores[nid] = scores.get(nid, 0) + weight * calibrated / (constant + rank)
            channels.setdefault(nid, {})["association"] = rank
        ordered = sorted(rows, key=lambda nid: ((mode == "exact" and nid not in direct_ids), -scores.get(nid, 0),
                         -graph.strength(graph.get(nid)), nid))
        if mode == "explore":
            # Deliberate autonomous exploration fills unused slots only. It
            # cannot randomly evict a normal factual result.
            quota = max(0, min(3, getattr(self.settings, "retrieval_exploration_k", 1)))
            exploration.sort(key=lambda item: (self.repo.usage.counts(item[0]).get("presented", 0), -item[1], item[0]))
            for nid, activation, chains in exploration[:quota]:
                rows[nid] = {"graph_activation": activation, "chain_ids": chains, "retrieval_role": "exploration"}
                scores[nid], channels[nid] = 0.0, {"exploration": 1}
                ordered.append(nid)
        output, families = [], set()
        for nid in ordered:
            node = graph.get(nid)
            family = graph.evidence_family(node)
            if families.intersection(family):
                continue
            view = graph.view(node, self.keys, query)
            if not view:
                continue
            row = {**rows[nid], **view}
            row["associations"] = graph.reasons(row.get("chain_ids") or [], node["scope"])
            families.update(family)
            output.append({**row, "score": scores[nid], "retrieval_channels": channels[nid]})
            if len(output) == k:
                break
        self.last_trace["graph_candidates"] = len(graph_ids)
        self.last_trace["graph_hops"] = self.settings.graph_max_hops if expansion_enabled else 0
        self.last_trace["graph_expansion"] = expansion_enabled
        competition = getattr(self.repo, "competition", None)
        output = self._describe(output, as_of)
        return competition.mint(query, output) if competition else output

    def _describe(self, rows, as_of):
        describe = getattr(self.repo.temporal, "describe_memory_ids", None)
        times = describe([row["memory_id"] for row in rows if row.get("memory_id")], as_of=as_of) if describe else {}
        for row in rows:
            if row.get("memory_id") in times:
                row["temporal"] = times[row["memory_id"]]
        return rows

    def _rerank(self, query, candidates):
        try:
            from sentence_transformers import CrossEncoder
            if not hasattr(self, "_reranker"):
                # 不在用户提问中下载模型；由部署流程预备本地目录。
                self._reranker = CrossEncoder(self.settings.reranker_model, local_files_only=True, device="cpu")
            scores = self._reranker.predict([(query, c["search_text"]) for c in candidates], show_progress_bar=False)
            if len(scores) != len(candidates) or any(not math.isfinite(float(s)) for s in scores):
                raise ValueError("重排器返回了无效分数")
            for candidate, score in zip(candidates, scores):
                candidate["score"] = float(score)
            candidates.sort(key=lambda c: (-c["score"], c["id"]))
            self.last_trace["reranker"] = "cross_encoder"
        except Exception:
            self.last_trace["reranker"] = "local_fallback"
            log.exception("本地重排模型不可用，使用 RRF 与主题覆盖排序")
        return candidates
