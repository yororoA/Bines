from __future__ import annotations

from typing import Any

from thinking_settings import thinking_settings
from utils.time_utils import day_key
from ..vector_store.chroma_store import (
    ChromaMemoryStore,
    get_memory_store,
    COLLECTION_KNOWLEDGE,
    COLLECTION_PERSONA,
    COLLECTION_SLICED_DIARY,
)


def retrieve_memories(
    query: str,
    knowledge_k: int | None = None,
    persona_k: int | None = None,
    diary_k: int | None = None,
    target_day_key: str | None = None,
    store: ChromaMemoryStore | None = None,
) -> dict[str, list[dict[str, Any]]]:
    memory_store = store or get_memory_store()
    _knowledge_k = knowledge_k if knowledge_k is not None else thinking_settings.RETRIEVAL_KNOWLEDGE_K
    _persona_k = persona_k if persona_k is not None else thinking_settings.RETRIEVAL_PERSONA_K
    _diary_k = diary_k if diary_k is not None else thinking_settings.RETRIEVAL_DIARY_K

    knowledge = memory_store.search(
        COLLECTION_KNOWLEDGE, query, k=_knowledge_k, target_day_key=day_key()
    )
    persona = memory_store.search(
        COLLECTION_PERSONA, query, k=_persona_k, target_day_key=day_key()
    )
    diary = memory_store.search(
        COLLECTION_SLICED_DIARY, query, k=_diary_k, target_day_key=day_key()
    )

    return {
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
        knowledge_k=5,
        persona_k=2,
        diary_k=1,
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