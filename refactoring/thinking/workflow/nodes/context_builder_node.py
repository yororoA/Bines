from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langchain.messages import HumanMessage

from ..status import GraphStatus

logger = logging.getLogger(__name__)
from memory import (
    PersonaState,
    retrieve_for_manager,
    format_retrieval_results,
    get_memory_store,
    COLLECTION_PERSONA,
    persona_cache,
)
from utils.time_utils import day_key
from utils import file_cache

USER_PROFILE_CATEGORIES = {"speaking_habits", "long_term_preferences", "tech_stack", "user_preferences", "user_name"}


def _load_soul_prompt() -> str:
    soul_path = Path(__file__).resolve().parents[2] / "Personal" / "SOUL.md"
    return file_cache.read(soul_path)


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
    cached = persona_cache.get()
    if cached is not None:
        return cached

    store = get_memory_store()
    persona_entries = store.get_all(COLLECTION_PERSONA)
    if not persona_entries:
        snapshot = PersonaState().to_dict()
        persona_cache.put(snapshot)
        return snapshot

    by_category: dict[str, dict[str, Any]] = {}
    for entry in persona_entries:
        meta = entry.get("metadata", {})
        cat = meta.get("category", "general")
        if cat not in USER_PROFILE_CATEGORIES:
            continue
        conf = meta.get("confidence", 0.5)
        existing = by_category.get(cat)
        if existing is None or conf > existing.get("confidence", 0):
            by_category[cat] = {
                "category": cat,
                "content": entry.get("content", ""),
                "confidence": conf,
            }

    user_name = by_category.get("user_name", {}).get("content", "")

    def _split_list(cat: str) -> list[str]:
        entry = by_category.get(cat)
        if not entry:
            return []
        return entry.get("content", "").split(";")

    snapshot = PersonaState(
        user_name=user_name,
        speaking_habits=_split_list("speaking_habits"),
        long_term_preferences=_split_list("long_term_preferences"),
        tech_stack=_split_list("tech_stack"),
        user_preferences=_split_list("user_preferences"),
    ).to_dict()
    persona_cache.put(snapshot)
    return snapshot


def _build_rag_recall(query: str) -> dict[str, Any]:
    if not query:
        return {}
    try:
        results = retrieve_for_manager(query)
        formatted = format_retrieval_results(results)
    except Exception:
        logger.exception("RAG retrieval failed, proceeding without context")
        return {}
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
    soul_prompt = _load_soul_prompt()
    return {
        "persona_snapshot": persona_snapshot,
        "rag_recall": rag_recall,
        "soul_prompt": soul_prompt,
    }