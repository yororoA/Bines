from __future__ import annotations

import logging
from typing import Any, Literal

from langchain.messages import SystemMessage
from langgraph.types import Command, Send

from utils import shared_langchain_model
from ..status import GraphStatus, ManagerRoute, PerformerInput, ReplyInput, MAX_ITERATIONS
from memory import PersonaState

logger = logging.getLogger(__name__)

_CONVERGENCE_WINDOW = 3


def _make_reply_input(
    state: GraphStatus,
    *,
    final: bool = True,
    message: str = "",
    soul_prompt: str = "",
) -> ReplyInput:
    return ReplyInput(
        tasks=[],
        Final=final,
        message=message,
        persona_snapshot=state.get("persona_snapshot", {}),
        already_said=state.get("already_said", []),
        soul_prompt=soul_prompt,
        rag_recall=state.get("rag_recall", {}),
    )


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


def _extract_task_count(thought: str) -> int | None:
    marker = "[task_count="
    start = thought.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = thought.find("]", start)
    if end == -1:
        return None
    try:
        return int(thought[start:end])
    except ValueError:
        return None


def _check_convergence(state: GraphStatus) -> bool:
    thoughts = state.get("thoughts", [])
    if len(thoughts) < _CONVERGENCE_WINDOW:
        return False
    recent = thoughts[-_CONVERGENCE_WINDOW:]
    counts = [_extract_task_count(t) for t in recent]
    if any(c is None for c in counts):
        return False
    return len(set(counts)) == 1


def _assemble_manager_context(state: GraphStatus) -> str:
    persona_snapshot = state.get("persona_snapshot", {})
    rag_recall = state.get("rag_recall", {})
    already_said = state.get("already_said", [])
    thoughts = state.get("thoughts", [])
    soul_prompt = state.get("soul_prompt", "")

    context_parts = []

    if soul_prompt:
        context_parts.append(soul_prompt)

    persona = PersonaState.from_dict(persona_snapshot)
    profile_str = persona.to_prompt_string()
    if profile_str:
        context_parts.append(profile_str)

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

    return "\n\n".join(context_parts)


def ManagerNode(
    state: GraphStatus,
) -> Command[Literal["performer", "advance_reply", "final_reply"]]:
    current_iteration = state.get("iteration_count", 0) + 1
    done_ids = _get_done_ids(state)
    task_count = len(done_ids)

    state_update: dict[str, Any] = {
        "iteration_count": current_iteration,
    }

    soul_prompt = state.get("soul_prompt", "")

    if current_iteration > 1 and _check_convergence(state):
        logger.info(
            "Convergence detected at iteration %d with %d tasks done",
            current_iteration, task_count,
        )
        reply_input = _make_reply_input(
            state,
            message=f"[system: all tasks completed, {task_count} tasks done]",
            soul_prompt=soul_prompt,
        )
        state_update["thoughts"] = [
            f"[Convergence] No new tasks for {_CONVERGENCE_WINDOW} iterations. "
            f"Total tasks: {task_count}"
        ]
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    if current_iteration >= MAX_ITERATIONS:
        reply_input = _make_reply_input(state, soul_prompt=soul_prompt)
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    context_str = _assemble_manager_context(state)
    invoke_messages = [SystemMessage(content=context_str)] + list(state.get("messages", []))
    try:
        result: ManagerRoute = shared_langchain_model.get_structured(ManagerRoute).invoke(invoke_messages)
    except Exception:
        logger.exception("ManagerModel invoke failed, falling back to final_reply")
        reply_input = _make_reply_input(state, soul_prompt=soul_prompt)
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    thought_with_count = f"{result.thoughts} [task_count={task_count}]"
    state_update["thoughts"] = [thought_with_count]

    if result.goto_final_reply:
        reply_input = _make_reply_input(
            state, message=result.final_reply_hint or "", soul_prompt=soul_prompt,
        )
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    if result.goto_advance_reply:
        reply_input = _make_reply_input(
            state, final=False, message=result.advance_reply_hint or "", soul_prompt=soul_prompt,
        )
        return Command(
            update=state_update,
            goto=[Send("advance_reply", reply_input)],
        )

    pending_tasks = [t for t in result.performer_tasks if t.task_id not in done_ids]
    if pending_tasks:
        return Command(
            update=state_update,
            goto=[Send("performer", PerformerInput(task_item=task, soul_prompt=soul_prompt)) for task in pending_tasks],
        )

    reply_input = _make_reply_input(state, soul_prompt=soul_prompt)
    return Command(
        update=state_update,
        goto=[Send("final_reply", reply_input)],
    )