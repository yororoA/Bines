from __future__ import annotations

from typing import Any

from ..vector_store.chroma_store import (
    ChromaMemoryStore,
    get_memory_store,
    COLLECTION_SUMMARY,
    COLLECTION_KNOWLEDGE,
    COLLECTION_PERSONA,
    COLLECTION_SLICED_DIARY,
)

DEFAULT_SUMMARY_K = 3
DEFAULT_KNOWLEDGE_K = 5
DEFAULT_PERSONA_K = 4
DEFAULT_DIARY_K = 2


def retrieve_memories(
    query: str,
    summary_k: int = DEFAULT_SUMMARY_K,
    knowledge_k: int = DEFAULT_KNOWLEDGE_K,
    persona_k: int = DEFAULT_PERSONA_K,
    diary_k: int = DEFAULT_DIARY_K,
    day_key: str | None = None,
    store: ChromaMemoryStore | None = None,
) -> dict[str, list[dict[str, Any]]]:
    memory_store = store or get_memory_store()

    summaries = memory_store.search_with_filter(
        COLLECTION_SUMMARY, query, k=summary_k, target_day_key=day_key
    )
    knowledge = memory_store.search_with_filter(
        COLLECTION_KNOWLEDGE, query, k=knowledge_k, target_day_key=day_key
    )
    persona = memory_store.search_with_filter(
        COLLECTION_PERSONA, query, k=persona_k, target_day_key=day_key
    )
    diary = memory_store.search_with_filter(
        COLLECTION_SLICED_DIARY, query, k=diary_k, target_day_key=day_key
    )

    return {
        "summaries": summaries,
        "knowledge": knowledge,
        "persona": persona,
        "diary": diary,
    }


def retrieve_for_reply(
    query: str,
    store: ChromaMemoryStore | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return retrieve_memories(
        query,
        summary_k=3,
        knowledge_k=3,
        persona_k=4,
        diary_k=2,
        store=store,
    )


def retrieve_for_manager(
    query: str,
    store: ChromaMemoryStore | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return retrieve_memories(
        query,
        summary_k=3,
        knowledge_k=5,
        persona_k=2,
        diary_k=1,
        store=store,
    )


def retrieve_for_performer(
    query: str,
    store: ChromaMemoryStore | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return retrieve_memories(
        query,
        summary_k=1,
        knowledge_k=3,
        persona_k=0,
        diary_k=0,
        store=store,
    )


def format_retrieval_results(results: dict[str, list[dict[str, Any]]]) -> str:
    parts = []
    for category, entries in results.items():
        if not entries:
            continue
        parts.append(f"[{category}]")
        for entry in entries:
            content = entry.get("content", "")
            score = entry.get("score", 0)
            parts.append(f"  (score={score:.3f}) {content}")
        parts.append("")
    return "\n".join(parts)