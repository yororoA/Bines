from __future__ import annotations

from typing import Any

from langchain.messages import HumanMessage

from ..status import GraphStatus
from memory import (
    PersonaState,
    retrieve_for_manager,
    format_retrieval_results,
    get_memory_store,
    COLLECTION_PERSONA,
)
from utils.time_utils import day_key


def _extract_query(state: GraphStatus) -> str:
    messages = state.get("messages", [])
    if not messages:
        return ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return msg.content or ""
    last = messages[-1]
    content = getattr(last, "content", "")
    if isinstance(content, str):
        return content
    return ""


def _load_persona(state: GraphStatus) -> dict[str, Any]:
    store = get_memory_store()
    persona_entries = store.get_all(COLLECTION_PERSONA)
    if not persona_entries:
        return PersonaState().to_dict()

    by_category: dict[str, dict[str, Any]] = {}
    for entry in persona_entries:
        meta = entry.get("metadata", {})
        cat = meta.get("category", "general")
        conf = meta.get("confidence", 0.5)
        existing = by_category.get(cat)
        if existing is None or conf > existing.get("confidence", 0):
            by_category[cat] = {
                "category": cat,
                "content": entry.get("content", ""),
                "confidence": conf,
            }

    tone = by_category.get("tone", {}).get("content", "friendly")
    style = by_category.get("style", {}).get("content", "concise")
    role_identity = by_category.get("role_identity", {}).get("content", "assistant")
    return PersonaState(
        tone=tone,
        style=style,
        role_identity=role_identity,
    ).to_dict()


def _build_rag_recall(query: str) -> dict[str, Any]:
    if not query:
        return {}
    results = retrieve_for_manager(query)
    formatted = format_retrieval_results(results)
    return {
        "query": query,
        "day_key": day_key(),
        "results": results,
        "formatted": formatted,
    }


def ContextBuilderNode(state: GraphStatus) -> dict:
    query = _extract_query(state)
    persona_snapshot = _load_persona(state)
    rag_recall = _build_rag_recall(query)
    return {
        "persona_snapshot": persona_snapshot,
        "rag_recall": rag_recall,
    }