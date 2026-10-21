"""Chroma vector index derived from SQLite. It can be deleted and rebuilt at any time."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .embedding import Embedder

COLLECTION = "memories"


def _metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    meta = {
        "kind": record["kind"],
        "scope": record["scope"],
        "importance": float(record.get("importance") or 0),
    }
    for key in ("conversation_id", "subject_id", "day_key"):
        if record.get(key):
            meta[key] = str(record[key])
    if record.get("end_ts") is not None:
        meta["end_ts"] = float(record["end_ts"])
    return meta


class VectorIndex:
    def __init__(self, index_dir: Path | str, embedder: Embedder):
        import chromadb

        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._client = chromadb.PersistentClient(path=str(self.index_dir))
        self._collection = self._open()

    def _open(self):
        return self._client.get_or_create_collection(
            name=COLLECTION, embedding_function=None, metadata={"hnsw:space": "cosine"}
        )

    def count(self) -> int:
        return int(self._collection.count())

    def upsert(self, records: Sequence[Dict[str, Any]]) -> List[str]:
        records = [r for r in records if (r.get("content") or "").strip()]
        if not records:
            return []
        embeddings = self.embedder.embed_documents([r["content"] for r in records])
        ids = [r["id"] for r in records]
        self._collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=[r["content"] for r in records],
            metadatas=[_metadata(r) for r in records],
        )
        return ids

    def delete(self, ids: Sequence[str]) -> None:
        if ids:
            self._collection.delete(ids=list(ids))

    def reset(self) -> None:
        try:
            self._client.delete_collection(COLLECTION)
        except Exception:
            pass
        self._collection = self._open()

    def query(
        self,
        text: str,
        scopes: Sequence[str],
        kinds: Optional[Sequence[str]] = None,
        n: int = 10,
        subject_id: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        """Return ``(memory_id, cosine_distance)`` pairs restricted to ``scopes``."""
        if not text.strip() or not scopes or self.count() == 0:
            return []
        clauses: List[Dict[str, Any]] = [{"scope": {"$in": list(scopes)}}]
        if kinds:
            clauses.append({"kind": {"$in": list(kinds)}})
        if subject_id:
            clauses.append({"subject_id": subject_id})
        where = clauses[0] if len(clauses) == 1 else {"$and": clauses}
        result = self._collection.query(
            query_embeddings=[self.embedder.embed_query(text)],
            n_results=max(1, min(n, self.count())),
            where=where,
            include=["distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [(memory_id, float(distance)) for memory_id, distance in zip(ids, distances)]
