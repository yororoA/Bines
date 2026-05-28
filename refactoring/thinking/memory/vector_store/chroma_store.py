from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

from thinking_settings import thinking_settings
from utils.time_utils import day_key

logger = logging.getLogger(__name__)

COLLECTION_SUMMARY = "summary"
COLLECTION_KNOWLEDGE = "knowledge"
COLLECTION_PERSONA = "persona"
COLLECTION_DIARY = "diary"
COLLECTION_SLICED_DIARY = "sliced_diary"
COLLECTION_BUFFER = "buffer"

ALL_COLLECTIONS = [
    COLLECTION_SUMMARY,
    COLLECTION_KNOWLEDGE,
    COLLECTION_PERSONA,
    COLLECTION_DIARY,
    COLLECTION_SLICED_DIARY,
    COLLECTION_BUFFER,
]

DECAY_HALF_LIFE_DAYS = 30.0
DECAY_MIN_IMPORTANCE = 1.0
DECAY_MAX_ENTRIES = 500

_global_embeddings: HuggingFaceEmbeddings | None = None


def _get_embeddings() -> HuggingFaceEmbeddings:
    global _global_embeddings
    if _global_embeddings is None:
        model_name = thinking_settings.RAG_EMBEDDING_MODEL
        model_kwargs = {}
        if thinking_settings.HF_ENDPOINT:
            model_kwargs["endpoint_url"] = thinking_settings.HF_ENDPOINT
        _global_embeddings = HuggingFaceEmbeddings(
            model_name=model_name, model_kwargs=model_kwargs
        )
    return _global_embeddings


class ChromaMemoryStore:
    def __init__(self, persist_dir: str | None = None):
        self._persist_dir = persist_dir or thinking_settings.RAG_PERSIST_DIR
        self._embeddings = _get_embeddings()
        self._collections: dict[str, Chroma] = {}
        for name in ALL_COLLECTIONS:
            self._collections[name] = Chroma(
                collection_name=name,
                embedding_function=self._embeddings,
                persist_directory=str(Path(self._persist_dir).resolve()),
            )

    def _get_collection(self, collection: str) -> Chroma:
        if collection not in self._collections:
            raise ValueError(
                f"Unknown collection '{collection}'. Must be one of {ALL_COLLECTIONS}"
            )
        return self._collections[collection]

    def add(
        self,
        collection: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        doc_id: str | None = None,
    ) -> str:
        store = self._get_collection(collection)
        _id = doc_id or uuid.uuid4().hex

        try:
            similar = store.similarity_search_with_score(content, k=1)
            if similar:
                _, score = similar[0]
                if score < thinking_settings.DEDUP_SIMILARITY_THRESHOLD:
                    logger.debug(
                        "Skipping duplicate in %s (score=%.4f < threshold=%.4f)",
                        collection, score, thinking_settings.DEDUP_SIMILARITY_THRESHOLD,
                    )
                    return _id
        except Exception:
            logger.debug("Dedup check failed for %s, proceeding with add", collection)

        meta = metadata or {}
        if "memory_type" not in meta:
            meta["memory_type"] = collection
        if "created_at" not in meta:
            meta["created_at"] = datetime.now().isoformat()
        if "day_key" not in meta:
            meta["day_key"] = day_key()
        store.add_texts(
            texts=[content],
            metadatas=[meta],
            ids=[_id],
        )
        return _id

    def search(
        self,
        collection: str,
        query: str,
        k: int = 3,
        filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        store = self._get_collection(collection)
        results = store.similarity_search_with_score(query, k=k, filter=filter)
        entries = []
        for doc, score in results:
            entries.append(
                {
                    "content": doc.page_content,
                    "metadata": doc.metadata,
                    "score": score,
                }
            )
        return entries

    def search_with_filter(
        self,
        collection: str,
        query: str,
        k: int = 3,
        target_day_key: str | None = None,
        extra_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        chroma_filter: dict[str, Any] = {}
        if target_day_key:
            chroma_filter["day_key"] = target_day_key
        if extra_filter:
            chroma_filter.update(extra_filter)
        if not chroma_filter:
            chroma_filter = None
        return self.search(collection, query, k=k, filter=chroma_filter)

    def get_all(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        store = self._get_collection(collection)
        results = store.get(include=["documents", "metadatas"], filter=filter)
        entries = []
        ids = results.get("ids", [])
        documents = results.get("documents", [])
        metadatas = results.get("metadatas", [])
        for i, doc_id in enumerate(ids):
            entry = {
                "id": doc_id,
                "content": documents[i] if i < len(documents) else "",
                "metadata": metadatas[i] if i < len(metadatas) else {},
            }
            entries.append(entry)
        return entries

    def count(self, collection: str) -> int:
        store = self._get_collection(collection)
        return len(store.get(include=[]).get("ids", []))

    def delete_by_ids(self, collection: str, ids: list[str]) -> None:
        store = self._get_collection(collection)
        store.delete(ids=ids)

    def decay_collection(
        self,
        collection: str,
        half_life_days: float = DECAY_HALF_LIFE_DAYS,
        min_effective_importance: float = DECAY_MIN_IMPORTANCE,
        max_entries: int = DECAY_MAX_ENTRIES,
    ) -> int:
        entries = self.get_all(collection)
        if not entries:
            return 0

        now = datetime.now()
        to_delete: list[str] = []
        scored: list[tuple[str, float]] = []

        for entry in entries:
            meta = entry.get("metadata", {})
            importance = meta.get("importance", 5.0)
            created_at_str = meta.get("created_at", "")
            try:
                created_at = datetime.fromisoformat(created_at_str)
            except (ValueError, TypeError):
                created_at = now

            age_days = (now - created_at).total_seconds() / 86400
            decay_factor = math.exp(-0.693 * age_days / half_life_days)
            effective_importance = importance * decay_factor

            if effective_importance < min_effective_importance:
                to_delete.append(entry["id"])
            else:
                scored.append((entry["id"], effective_importance))

        if len(scored) > max_entries:
            scored.sort(key=lambda x: x[1])
            excess = len(scored) - max_entries
            to_delete.extend(entry_id for entry_id, _ in scored[:excess])

        if to_delete:
            self.delete_by_ids(collection, to_delete)
            logger.info(
                "Decayed %s: removed %d entries", collection, len(to_delete)
            )

        return len(to_delete)

    def get_collection_names(self) -> list[str]:
        return list(self._collections.keys())


_global_store: ChromaMemoryStore | None = None


def get_memory_store() -> ChromaMemoryStore:
    global _global_store
    if _global_store is None:
        _global_store = ChromaMemoryStore()
    return _global_store