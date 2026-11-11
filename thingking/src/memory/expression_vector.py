"""Semantic retrieval for expression samples, backed by a dedicated Qdrant collection.

The expression corpus is intentionally isolated from the main memory database:
Qdrant stores only vectors and small, public-safe payload fields.  If the
service is unavailable the caller falls back to the lexical expression store.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from .embedding import Embedder
from .expression_learning import SITUATIONS, TECHNIQUES

log = logging.getLogger("bines.memory.expression_vector")

_NAMESPACE = uuid.UUID("0d6f0a0e-7d3a-5a4a-9a38-2c3f4b5d6e7f")
_MAX_INDEX_CHARS = 320
_MAX_SAMPLE_CHARS = 80
_MAX_CONTEXT_CHARS = 120
_UPSERT_BATCH = 64


class ExpressionVectorError(RuntimeError):
    """Qdrant is unavailable or returned an unusable response."""


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def _cosine(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(left, right))
    left_norm = math.sqrt(sum(float(x) * float(x) for x in left))
    right_norm = math.sqrt(sum(float(y) * float(y) for y in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def load_corpus(path: str | Path) -> List[Dict[str, Any]]:
    """Load and validate a curated expression JSONL file."""
    source = Path(path)
    records: List[Dict[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        item = json.loads(line)
        required = ("quote", "context", "technique", "situation", "source", "source_id")
        missing = [key for key in required
                   if not isinstance(item.get(key), str) or not item[key].strip()]
        if missing:
            raise ValueError(f"{source}:{line_number} 缺少字段 {missing}")
        if item["technique"] not in TECHNIQUES or item["situation"] not in SITUATIONS:
            raise ValueError(f"{source}:{line_number} technique/situation 不在允许枚举内")
        if item["quote"] not in item["context"]:
            raise ValueError(f"{source}:{line_number} quote 不在 context 中")
        records.append(item)
    if not records:
        raise ValueError(f"{source} 语料为空")
    return records


class ExpressionVectorStore:
    """A small Qdrant-backed semantic index for the owner's expression corpus."""

    def __init__(self, url: str, collection: str, embedder: Embedder, *,
                 timeout: float = 2.0, min_score: float = 0.35, top_k: int = 12):
        self.url = str(url or "").rstrip("/")
        self.collection = str(collection or "bines_expression")
        self.embedder = embedder
        self.timeout = max(0.2, float(timeout))
        self.min_score = float(min_score)
        self.top_k = max(1, min(50, int(top_k)))
        self._session = requests.Session()
        self._lock = threading.RLock()
        self._ready = False
        self._dimensions: Optional[int] = None
        self.last_error = ""

    # ---------- transport ----------

    def _request(self, method: str, path: str, *, allow_404: bool = False, **kwargs):
        try:
            response = self._session.request(method, self.url + path,
                                             timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            self.last_error = str(exc)
            raise ExpressionVectorError(f"Qdrant {method} {path} failed: {exc}") from exc
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            self.last_error = response.text[:300]
            raise ExpressionVectorError(
                f"Qdrant {method} {path} -> {response.status_code}: {response.text[:300]}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def health(self) -> bool:
        try:
            self._request("GET", "/healthz")
            return True
        except ExpressionVectorError:
            return False

    # ---------- collection lifecycle ----------

    def ensure_collection(self) -> None:
        with self._lock:
            if self._ready:
                return
            info = self._request("GET", f"/collections/{self.collection}", allow_404=True)
            dimensions = len(self.embedder.embed_query("expression vector dimension probe"))
            if not isinstance(dimensions, int) or dimensions <= 0:
                raise ExpressionVectorError("embedding dimension is unavailable")
            if info is None:
                self._request("PUT", f"/collections/{self.collection}", json={
                    "vectors": {"size": dimensions, "distance": "Cosine"},
                })
            else:
                vectors = (((info.get("result") or {}).get("config") or {})
                           .get("params") or {}).get("vectors")
                if isinstance(vectors, dict):
                    size, distance = vectors.get("size"), str(vectors.get("distance") or "").lower()
                    if size != dimensions or distance != "cosine":
                        raise ExpressionVectorError(
                            f"Qdrant collection {self.collection} mismatch: "
                            f"size={size}, distance={distance}, expected={dimensions}/cosine")
            self._dimensions = dimensions
            self._ready = True

    def status(self) -> Dict[str, Any]:
        return {"url": self.url, "collection": self.collection, "ready": self._ready,
                "dimensions": self._dimensions, "last_error": self.last_error}

    def count(self, scope: Optional[str] = None) -> int:
        self.ensure_collection()
        body: Dict[str, Any] = {"exact": True}
        if scope:
            body["filter"] = {"must": [{"key": "scope", "match": {"value": scope}}]}
        data = self._request("POST", f"/collections/{self.collection}/points/count", json=body)
        return int((data.get("result") or {}).get("count") or 0)

    # ---------- indexing ----------

    @staticmethod
    def _point_id(scope: str, item: Dict[str, Any]) -> str:
        key = "|".join([scope, str(item.get("source_id") or ""), str(item.get("quote") or ""),
                        str(item.get("technique") or ""), str(item.get("situation") or "")])
        return str(uuid.uuid5(_NAMESPACE, key))

    @staticmethod
    def _index_text(item: Dict[str, Any]) -> str:
        situation = SITUATIONS.get(item["situation"], item["situation"])
        # Embed the response itself, not the whole transcript: long contexts
        # dilute short emotional queries and create technical false positives.
        return _clip(f"{situation}。{item['quote']}", _MAX_INDEX_CHARS)

    @staticmethod
    def _payload(scope: str, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "scope": scope,
            "quote": _clip(item["quote"], _MAX_SAMPLE_CHARS),
            "context": _clip(item["context"], _MAX_CONTEXT_CHARS),
            "technique": item["technique"],
            "situation": item["situation"],
            "source": str(item.get("source") or ""),
            "source_id": str(item.get("source_id") or ""),
            "source_title": str(item.get("source_title") or ""),
            "quality": float(item.get("quality") or 0.0),
            "created_at": float(item.get("created_at") or 0.0),
        }

    def index(self, items: Iterable[Dict[str, Any]], scope: str = "owner") -> int:
        records = list(items)
        if not records:
            return 0
        self.ensure_collection()
        texts = [self._index_text(item) for item in records]
        vectors = self.embedder.embed_documents(texts)
        if len(vectors) != len(records):
            raise ExpressionVectorError("embedding result count mismatch")
        points = []
        for item, vector in zip(records, vectors):
            if len(vector) != self._dimensions:
                raise ExpressionVectorError("embedding dimension mismatch")
            points.append({
                "id": self._point_id(scope, item),
                "vector": [float(x) for x in vector],
                "payload": self._payload(scope, item),
            })
        for start in range(0, len(points), _UPSERT_BATCH):
            self._request("PUT", f"/collections/{self.collection}/points?wait=true",
                          json={"points": points[start:start + _UPSERT_BATCH]})
        return len(points)

    def delete_scope(self, scope: str = "owner") -> None:
        self.ensure_collection()
        self._request("POST", f"/collections/{self.collection}/points/delete?wait=true",
                      json={"filter": {"must": [{"key": "scope",
                                                 "match": {"value": scope}}]}})

    def reindex(self, items: Iterable[Dict[str, Any]], scope: str = "owner") -> int:
        self.ensure_collection()
        self.delete_scope(scope)
        return self.index(items, scope)

    # ---------- retrieval ----------

    @staticmethod
    def _mmr(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        remaining = list(candidates)
        while remaining and len(selected) < limit:
            best, best_value = None, float("-inf")
            for item in remaining:
                similarity = max((_cosine(item["_vector"], chosen["_vector"])
                                  for chosen in selected), default=0.0)
                value = 0.75 * item["score"] - 0.25 * similarity + 0.01 * item["quality"]
                if value > best_value:
                    best, best_value = item, value
            if best is None:
                break
            remaining.remove(best)
            selected.append(best)
        for item in selected:
            item.pop("_vector", None)
        return selected

    def search(self, scope: str, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        text = " ".join(str(query or "").split())
        if not text:
            return []
        self.ensure_collection()
        vector = self.embedder.embed_query(text)
        if len(vector) != self._dimensions:
            raise ExpressionVectorError("query embedding dimension mismatch")
        body = {
            "vector": [float(x) for x in vector],
            "limit": self.top_k,
            "with_payload": True,
            "with_vector": True,
            "score_threshold": self.min_score,
            "filter": {"must": [{"key": "scope", "match": {"value": scope}}]},
        }
        data = self._request("POST", f"/collections/{self.collection}/points/search",
                             json=body)
        candidates: List[Dict[str, Any]] = []
        for hit in data.get("result") or []:
            payload = hit.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            technique, situation = payload.get("technique"), payload.get("situation")
            if technique not in TECHNIQUES or situation not in SITUATIONS:
                continue
            hit_vector = hit.get("vector")
            if not isinstance(hit_vector, list) or len(hit_vector) != self._dimensions:
                continue
            score = float(hit.get("score") or 0.0)
            if score < self.min_score:
                continue
            candidates.append({
                "id": str(hit.get("id") or ""),
                "technique": technique,
                "situation": situation,
                "sample": _clip(payload.get("quote") or "", _MAX_SAMPLE_CHARS),
                "context": _clip(payload.get("context") or "", _MAX_CONTEXT_CHARS),
                "score": score,
                "quality": float(payload.get("quality") or 0.0),
                "source": str(payload.get("source") or ""),
                "source_title": str(payload.get("source_title") or ""),
                "_vector": hit_vector,
            })
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return self._mmr(candidates, max(1, min(3, int(limit))))
