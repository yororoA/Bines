from __future__ import annotations

from typing import Any, Literal

from langchain.messages import SystemMessage
from langgraph.types import Command, Send

from utils import generate_langchain_model
from thinking_settings import thinking_settings
from ..status import GraphStatus, ManagerRoute, ReplyInput, MAX_ITERATIONS
from memory import PersonaState

_model = generate_langchain_model(thinking_settings.MODEL_SELECTED)
ManagerModel = _model.with_structured_output(ManagerRoute)


def _get_done_ids(state: GraphStatus) -> set[str]:
    done_ids = set()
    for items in state.get("tasks_done", {}).values():
        if not items:
            continue
        for item in items:
            task_id = getattr(item, "task_id", None) or (
                item.get("task_id") if isinstance(item, dict) else None
            )
            if task_id:
                done_ids.add(task_id)
    return done_ids


def _assemble_manager_context(state: GraphStatus) -> list[Any]:
    messages = list(state.get("messages", []))
    persona_snapshot = state.get("persona_snapshot", {})
    rag_recall = state.get("rag_recall", {})
    already_said = state.get("already_said", [])
    thoughts = state.get("thoughts", [])
    soul_prompt = state.get("soul_prompt", "")

    context_parts = []

    if soul_prompt:
        context_parts.append(soul_prompt)

    persona = PersonaState.from_dict(persona_snapshot)
    context_parts.append(
        f"[Persona] Tone: {persona.tone}, Style: {persona.style}, "
        f"Role: {persona.role_identity}"
    )

    if rag_recall and "formatted" in rag_recall:
        context_parts.append(f"[RAG Context]\n{rag_recall['formatted']}")

    if thoughts:
        recent = thoughts[-5:]
        context_parts.append(
            "[Your Previous Thoughts]\n" + "\n---\n".join(recent)
        )

    if already_said:
        context_parts.append(
            "[Already Said] " + "; ".join(already_said[-5:])
        )

    context_str = "\n\n".join(context_parts)
    if context_str:
        messages.insert(0, SystemMessage(content=context_str))

    return messages


def ManagerNode(
    state: GraphStatus,
) -> Command[Literal["performer", "advance_reply", "final_reply"]]:
    current_iteration = state.get("iteration_count", 0) + 1
    context_messages = _assemble_manager_context(state)
    result: ManagerRoute = ManagerModel.invoke(context_messages)
    state_update: dict[str, Any] = {
        "thoughts": [result.thoughts],
        "iteration_count": current_iteration,
    }

    soul_prompt = state.get("soul_prompt", "")
    if current_iteration >= MAX_ITERATIONS:
        reply_input = ReplyInput(
            tasks=[],
            Final=True,
            message="",
            persona_snapshot=state.get("persona_snapshot", {}),
            already_said=state.get("already_said", []),
            soul_prompt=soul_prompt,
        )
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    if result.goto_final_reply:
        reply_input = ReplyInput(
            tasks=[],
            Final=True,
            message=result.final_reply_hint or "",
            persona_snapshot=state.get("persona_snapshot", {}),
            already_said=state.get("already_said", []),
            soul_prompt=soul_prompt,
        )
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    if result.goto_advance_reply:
        reply_input = ReplyInput(
            tasks=[],
            Final=False,
            message=result.advance_reply_hint or "",
            persona_snapshot=state.get("persona_snapshot", {}),
            already_said=state.get("already_said", []),
            soul_prompt=soul_prompt,
        )
        return Command(
            update=state_update,
            goto=[Send("advance_reply", reply_input)],
        )

    if result.performer_task is not None:
        done_ids = _get_done_ids(state)
        if result.performer_task.task_id not in done_ids:
            return Command(
                update=state_update,
                goto=[Send("performer", result.performer_task)],
            )

    reply_input = ReplyInput(
        tasks=[],
        Final=True,
        message="",
        persona_snapshot=state.get("persona_snapshot", {}),
        already_said=state.get("already_said", []),
        soul_prompt=soul_prompt,
    )
    return Command(
        update=state_update,
        goto=[Send("final_reply", reply_input)],
    )