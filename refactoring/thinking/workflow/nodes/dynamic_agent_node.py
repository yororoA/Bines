from __future__ import annotations

import logging
from typing import Any

from ..status import GraphStatus
from memory import (
    PersonaState,
    MemoryJudgment,
    judge_and_store,
    add_to_buffer,
    consolidate_buffer_to_diary,
    get_memory_store,
    COLLECTION_KNOWLEDGE,
    COLLECTION_DIARY,
)
from utils.time_utils import day_key
from utils import get_metrics_collector

logger = logging.getLogger(__name__)

_DECAY_INTERVAL: int = 5


def _collect_messages_text(state: GraphStatus) -> str:
    messages = state.get("messages", [])
    parts = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content:
            parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def _collect_tasks_summary(state: GraphStatus) -> str:
    tasks_done = state.get("tasks_done", {})
    parts = []
    for agent_name, items in tasks_done.items():
        for item in items:
            task_id = getattr(item, "task_id", "unknown")
            desc = getattr(item, "description", "")
            parts.append(f"[{agent_name}:{task_id}] {desc}")
    return "\n".join(parts)


def _collect_already_said(state: GraphStatus) -> str:
    already_said = state.get("already_said", [])
    if not already_said:
        return ""
    return "Already said: " + "; ".join(already_said)


def _extract_context_for_memory(state: GraphStatus) -> str:
    msg_text = _collect_messages_text(state)
    tasks_text = _collect_tasks_summary(state)
    already_said_text = _collect_already_said(state)

    parts = []
    if msg_text:
        parts.append(msg_text)
    if tasks_text:
        parts.append(tasks_text)
    if already_said_text:
        parts.append(already_said_text)
    return "\n\n".join(parts)


def _should_trigger_diary(diary_triggered_day: str) -> bool:
    current_day = day_key()
    return diary_triggered_day != current_day


def _run_memory_decay():
    store = get_memory_store()
    for col in (COLLECTION_KNOWLEDGE, COLLECTION_DIARY):
        try:
            removed = store.decay_collection(col)
            if removed:
                logger.info("Decay: removed %d entries from %s", removed, col)
        except Exception:
            logger.exception("Failed to decay collection %s", col)


def DynamicAgentNode(state: GraphStatus) -> dict[str, Any]:
    invocation_count = state.get("invocation_count", 0) + 1
    diary_triggered_day = state.get("diary_triggered_day", "")
    new_diary_triggered_day = diary_triggered_day

    collector = get_metrics_collector()

    with collector.track_node("dynamic_agent") as metrics:
        context_text = _extract_context_for_memory(state)

        if context_text:
            try:
                persona = PersonaState.from_dict(
                    state.get("persona_snapshot", {})
                )
                judgment: MemoryJudgment = judge_and_store(
                    context_text, persona=persona
                )

                if judgment.should_store and judgment.memory_type == "buffer":
                    add_to_buffer(
                        judgment.rewritten_content or context_text,
                        metadata={"topic": judgment.topic, "importance": judgment.importance},
                    )

                metrics.token_estimate += len(context_text) // 4
            except Exception:
                logger.exception("Memory judgment/storage failed")

        if _should_trigger_diary(diary_triggered_day):
            try:
                result = consolidate_buffer_to_diary(day_key())
                if result:
                    new_diary_triggered_day = day_key()
            except Exception:
                logger.exception("Diary consolidation failed")

        if invocation_count % _DECAY_INTERVAL == 0:
            _run_memory_decay()

    return {
        "diary_triggered_day": new_diary_triggered_day,
        "invocation_count": invocation_count,
    }
