from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from thinking_settings import thinking_settings
from ..vector_store.chroma_store import (
    ChromaMemoryStore,
    get_memory_store,
    COLLECTION_KNOWLEDGE,
    COLLECTION_PERSONA,
    COLLECTION_DIARY,
)
from ..persona_state import PersonaState, persona_cache
from ..lifecycle.buffer_diary import add_to_buffer
from utils import generate_langchain_model
from utils.time_utils import day_key

logger = logging.getLogger(__name__)

MemoryType = Literal["knowledge", "persona", "diary", "buffer"]


class MemoryJudgment(BaseModel):
    should_store: bool = Field(
        description="Whether this content is worth storing in long-term memory."
    )
    memory_type: MemoryType = Field(
        description="Which memory type: summary, knowledge, persona, or diary."
    )
    importance: float = Field(
        default=5.0,
        ge=0.0,
        le=10.0,
        description="Importance score from 0 to 10.",
    )
    topic: str = Field(
        default="",
        description="Brief topic label for this memory entry.",
    )
    domain: str = Field(
        default="",
        description="Knowledge domain, e.g. 'programming', 'design'. Only for knowledge type.",
    )
    source: str = Field(
        default="",
        description="Origin of this memory, e.g. 'conversation', 'task_result'. Only for knowledge type.",
    )
    category: str = Field(
        default="",
        description="Persona category, e.g. 'preference', 'habit', 'tech_stack'. Only for persona type.",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence level for persona traits. Only for persona type.",
    )
    stability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Stability of the persona trait. Only for persona type.",
    )
    rewritten_content: str = Field(
        default="",
        description="Cleaned and rewritten version of the content for storage.",
    )


_JUDGE_MODEL = None
_JUDGE_MODEL_NAME: str = ""


def _get_judge_model():
    global _JUDGE_MODEL, _JUDGE_MODEL_NAME
    current_name = thinking_settings.MODEL_SELECTED
    if _JUDGE_MODEL is None or current_name != _JUDGE_MODEL_NAME:
        base_model = generate_langchain_model(current_name)
        _JUDGE_MODEL = base_model.with_structured_output(MemoryJudgment)
        _JUDGE_MODEL_NAME = current_name
    return _JUDGE_MODEL


_JUDGE_PROMPT_TEMPLATE = (
    "You are a memory judge for an AI agent system. "
    "Your job is to decide whether a piece of conversational content is worth "
    "storing in long-term memory, and if so, which type of memory it belongs to.\n\n"
    "Memory types:\n"
    "- knowledge: Stable, reusable knowledge — technical facts, project knowledge, "
    "user's long-term work knowledge extracted from conversations.\n"
    "- persona: Long-term stable information about the user — preferences, habits, "
    "tech stack, style preferences, stable identity traits. NOT temporary questions.\n"
    "- diary: Personal diary entries, emotional reflections, daily experiences.\n\n"
    "Current persona context:\n{persona_context}\n\n"
    "Content to judge:\n{content}\n\n"
    "Decide: should this be stored? If yes, which type, importance level, "
    "and provide a cleaned/rewritten version suitable for retrieval."
)


def judge_and_store(
    content: str,
    persona: PersonaState | None = None,
    metadata: dict[str, Any] | None = None,
    store: ChromaMemoryStore | None = None,
) -> MemoryJudgment:
    memory_store = store or get_memory_store()

    persona_context = persona.to_prompt_string() if persona else ""

    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        persona_context=persona_context or "No persona context available.",
        content=content,
    )

    model = _get_judge_model()
    judgment: MemoryJudgment = model.invoke(prompt)

    if not judgment.should_store:
        return judgment

    if judgment.memory_type == "buffer":
        add_to_buffer(
            judgment.rewritten_content or content,
            metadata={"topic": judgment.topic, "importance": judgment.importance},
        )
        return judgment

    final_content = judgment.rewritten_content or content
    final_meta = metadata or {}
    final_meta["importance"] = judgment.importance
    final_meta["topic"] = judgment.topic
    final_meta["day_key"] = day_key()
    final_meta["created_at"] = datetime.now().isoformat()

    if judgment.memory_type == "knowledge":
        final_meta["domain"] = judgment.domain
        final_meta["source"] = judgment.source
    elif judgment.memory_type == "persona":
        final_meta["category"] = judgment.category
        final_meta["confidence"] = judgment.confidence
        final_meta["stability"] = judgment.stability
        final_meta["updated_at"] = datetime.now().isoformat()

    collection_map = {
        "knowledge": COLLECTION_KNOWLEDGE,
        "persona": COLLECTION_PERSONA,
        "diary": COLLECTION_DIARY,
    }

    target_collection = collection_map[judgment.memory_type]
    memory_store.add(target_collection, final_content, metadata=final_meta)

    if judgment.memory_type == "persona" and judgment.category:
        _cleanup_low_confidence_persona(
            memory_store, judgment.category, judgment.confidence
        )
        persona_cache.invalidate()
        logger.debug("Persona cache invalidated after storing category=%s", judgment.category)

    return judgment


def _cleanup_low_confidence_persona(
    store: ChromaMemoryStore,
    category: str,
    new_confidence: float,
) -> None:
    entries = store.get_all(
        COLLECTION_PERSONA, filter={"category": category}
    )
    ids_to_delete = [
        e["id"] for e in entries
        if e.get("metadata", {}).get("confidence", 0) < new_confidence
    ]
    if ids_to_delete:
        store.delete_by_ids(COLLECTION_PERSONA, ids_to_delete)