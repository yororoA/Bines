from __future__ import annotations

from typing import Any

from ..status import GraphStatus
from memory import (
    PersonaState,
    MemoryJudgment,
    judge_and_store,
    add_to_buffer,
    get_existing_diary_day_keys,
    consolidate_buffer_to_diary,
)
from utils.time_utils import day_key


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


_DIARY_TRIGGERED_TODAY: str | None = None


def _should_trigger_diary() -> bool:
    current_day = day_key()
    if _DIARY_TRIGGERED_TODAY == current_day:
        return False
    existing_days = get_existing_diary_day_keys()
    return current_day not in existing_days


def DynamicAgentNode(state: GraphStatus) -> dict[str, Any]:
    global _DIARY_TRIGGERED_TODAY
    context_text = _extract_context_for_memory(state)

    if context_text:
        persona = PersonaState.from_dict(
            state.get("persona_snapshot", {})
        )
        judgment: MemoryJudgment = judge_and_store(
            context_text, persona=persona
        )

        if judgment.should_store and judgment.memory_type == "summary":
            add_to_buffer(
                judgment.rewritten_content or context_text,
                metadata={"topic": judgment.topic, "importance": judgment.importance},
            )

    if _should_trigger_diary():
        result = consolidate_buffer_to_diary(day_key())
        if result:
            _DIARY_TRIGGERED_TODAY = day_key()

    return {}