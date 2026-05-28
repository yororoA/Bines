from __future__ import annotations

from pathlib import Path
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

_SOUL_PROMPT: str | None = None


def _load_soul_prompt() -> str:
    global _SOUL_PROMPT
    if _SOUL_PROMPT is not None:
        return _SOUL_PROMPT
    soul_path = Path(__file__).resolve().parents[2] / "Personal" / "SOUL.md"
    if soul_path.exists():
        _SOUL_PROMPT = soul_path.read_text(encoding="utf-8")
    else:
        _SOUL_PROMPT = ""
    return _SOUL_PROMPT


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

    def _get(cat: str, default: str) -> str:
        return by_category.get(cat, {}).get("content", default)

    return PersonaState(
        tone=_get("tone", "friendly"),
        style=_get("style", "concise"),
        verbosity=_get("verbosity", "moderate"),
        role_identity=_get("role_identity", "assistant"),
        speaking_habits=by_category.get("speaking_habits", {}).get("content", "").split(";") if by_category.get("speaking_habits") else [],
        interaction_strategy=_get("interaction_strategy", "collaborative"),
        long_term_preferences=by_category.get("long_term_preferences", {}).get("content", "").split(";") if by_category.get("long_term_preferences") else [],
        tech_stack=by_category.get("tech_stack", {}).get("content", "").split(";") if by_category.get("tech_stack") else [],
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
    soul_prompt = _load_soul_prompt()
    return {
        "persona_snapshot": persona_snapshot,
        "rag_recall": rag_recall,
        "soul_prompt": soul_prompt,
    }