"""统一检索：全文与语义候选以 RRF 融合，重排后返回有来源的原文证据。"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime

from .evidence import EvidenceStore, _tokens
from .lexical_index import query_terms
from .models import MEMORY_KINDS

log = logging.getLogger("bines.memory.retrieval")


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
            when = datetime(int(match[1]), int(match[2]), int(match[3] or 1)).timestamp()
        except ValueError:
            pass
    history = any(word in query for word in ("以前", "曾经", "之前", "历史", "当时", "变化", "为什么"))
    return when, history or when is not None


class HybridRetriever:
    def __init__(self, repo, index, settings):
        self.repo, self.index, self.settings = repo, index, settings
        self.keys = EvidenceStore(repo)
        self._synced_changes = -1
        self.last_trace = {}

    def sync(self, vectors=False, rebuild=False):
        if rebuild:
            self.keys.clear_index_marks()
        if self._synced_changes != self.repo.conn.total_changes or rebuild:
            result = self.keys.sync()
            self._synced_changes = self.repo.conn.total_changes
        else:
            result = {"added": 0, "updated": 0, "deleted": 0}
        result["vectors"] = 0
        if vectors and hasattr(self.index, "upsert_keys"):
            try:
                alive = set(self.keys.all_ids())
                obsolete = set(self.index.key_ids()) - alive
                self.index.delete_keys(list(obsolete))
                while batch := self.keys.unindexed_keys():
                    ids = self.index.upsert_keys(batch)
                    self.keys.mark_indexed(ids)
                    result["vectors"] += len(ids)
            except Exception:
                log.exception("检索键向量更新失败，保留待重建标记，全文检索继续可用")
        self._synced_changes = self.repo.conn.total_changes
        return result

    def search(self, query, scopes, kinds=None, k=10, excluded=(), as_of=None, include_history=False):
        if not isinstance(query, str) or not query.strip() or not scopes:
            return []
        k = max(1, min(int(k), 50))
        count = max(k, self.settings.retrieval_candidates)
        scopes = list(dict.fromkeys(scopes))
        self.sync()
        # 原始消息与派生记忆都可命中；限定 semantic 的画像查询不混入聊天正文。
        allowed_kinds = list(kinds) if kinds else [*MEMORY_KINDS, "message"]
        terms = query_terms(query)
        topic = " ".join(sorted(terms)) or query
        lexical = self.keys.bm25_query(topic, scopes, allowed_kinds, count)
        rankings = {"bm25": [key for key, _ in lexical]}
        vector_distances = {}
        if hasattr(self.index, "query_keys"):
            try:
                vector = self.index.query_keys(query, scopes, allowed_kinds, count)
                vector_distances = dict(vector)
                rankings["vector"] = [key for key, _ in vector]
            except Exception:
                log.exception("向量检索失败，使用全文候选")
        elif getattr(self.index, "persistent", True):
            # 兼容旧索引或注入的测试索引；生产新索引优先使用独立片段向量。
            try:
                vector = self.index.query(query, scopes, [x for x in allowed_kinds if x != "message"], n=count)
                rankings["vector"] = [f"m:{memory_id}:0" for memory_id, _ in vector]
                vector_distances = {f"m:{memory_id}:0": distance for memory_id, distance in vector}
            except Exception:
                log.exception("旧向量索引不可用")
        scores, channels = rrf(rankings, self.settings.retrieval_rrf_constant)
        keys = self.keys.get_keys(list(scores), scopes)
        date, historical = time_intent(query)
        historical = include_history or historical
        visible = set(self.repo.temporal.visible_memory_ids(
            [row["memory_id"] for row in keys.values() if row["memory_id"]],
            as_of=as_of, include_history=historical,
        ))
        excluded = set(excluded)
        candidates = []
        for key_id, row in keys.items():
            source_id = row["memory_id"] or f"message:{row['message_id']}"
            if source_id in excluded or (row["memory_id"] and row["memory_id"] not in visible):
                continue
            content = row["search_text"].lower()
            coverage = sum(term in content for term in terms) / max(1, len(terms))
            # 向量通道独立命中允许同义改写；全文通道至少需要一个主题命中。
            if not coverage and vector_distances.get(key_id, 1.0) > self.settings.retrieval_max_distance:
                continue
            # 默认透明、确定性的局部重排；可选 CrossEncoder 在有限候选内做语义重排。
            score = scores[key_id] + coverage / (self.settings.retrieval_rrf_constant + 1)
            if date is not None and datetime.fromtimestamp(date).strftime("%Y-%m") in content:
                score += 0.002
            row = {**row, "rrf_score": scores[key_id], "score": score,
                   "retrieval_channels": channels[key_id]}
            candidates.append(row)
        candidates.sort(key=lambda row: (-row["score"], row["id"]))
        self.last_trace = {"candidates": len(candidates), "channels": {name: len(ids) for name, ids in rankings.items()},
                           "reranker": "local", "as_of": as_of, "historical": historical}
        if self.settings.reranker_model and candidates:
            candidates = self._rerank(query, candidates[:count])
        output, seen = [], set()
        for row in candidates:
            source_id = (row["source_type"], row["memory_id"] or row["message_id"])
            if source_id in seen:
                continue
            evidence = self.keys.evidence(row, scopes, query=query, limit=self.settings.evidence_limit)
            if not evidence:
                continue
            seen.add(source_id)
            memory = self.repo.get_memory(row["memory_id"]) if row["memory_id"] else None
            # key.content 是原文连续片段；evidence 指向原始轮次，不能把提取事实当成用户原话。
            output.append({
                **(memory or {}), "id": row["memory_id"] or f"message:{row['message_id']}",
                "memory_id": row["memory_id"], "message_id": row["message_id"],
                "key_id": row["id"], "scope": row["scope"], "kind": row["kind"],
                "content": row["content"], "evidence": evidence,
                "score": row["score"], "rrf_score": row["rrf_score"],
                "retrieval_channels": row["retrieval_channels"], "origin": row["meta"].get("origin"),
            })
            if len(output) == k:
                break
        return output

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
