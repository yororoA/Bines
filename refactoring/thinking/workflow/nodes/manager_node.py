from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from langchain.messages import SystemMessage
from langgraph.types import Command, Send

from utils import shared_langchain_model
from ..status import GraphStatus, ManagerRoute, PerformerInput, ReplyInput, TaskItem, MAX_ITERATIONS
from ..cancel import get_cancel_event
from ..context_manager import get_context_manager
from memory import PersonaState

logger = logging.getLogger(__name__)


def _get_convergence_window() -> int:
    from thinking_settings import thinking_settings
    return thinking_settings.CONVERGENCE_WINDOW


def _make_reply_input(
    *,
    final: bool = True,
    message: str = "",
    tasks: list | None = None,
) -> ReplyInput:
    return ReplyInput(
        Final=final,
        message=message,
        tasks=tasks or [],
    )


_REPLY_CATEGORIES = {"advance_reply", "final_reply", "reply_cancel", "reply_error"}


def _build_reply_tasks_from_state(state: GraphStatus, hint: str = "") -> list[TaskItem]:
    tasks: list[TaskItem] = []
    for category, items in state.get("tasks_done", {}).items():
        if category in _REPLY_CATEGORIES:
            continue
        for item in items:
            desc = getattr(item, "description", None) or (
                item.get("description") if isinstance(item, dict) else None
            )
            tid = getattr(item, "task_id", None) or (
                item.get("task_id") if isinstance(item, dict) else None
            )
            if desc:
                tasks.append(TaskItem(task_id=tid or f"ctx_{len(tasks)}", description=desc))
    instruction = hint or "Reply to the user's latest message based on conversation history and any context above."
    tasks.append(TaskItem(task_id="reply_instruction", description=instruction))
    return tasks


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


def _check_convergence(state: GraphStatus, current_task_count: int) -> tuple[bool, int]:
    last_count = state.get("last_task_count", -1)
    counter = state.get("convergence_counter", 0)
    if current_task_count == last_count and current_task_count >= 0:
        counter += 1
    else:
        counter = 0
    return counter >= _get_convergence_window(), counter


def _assemble_manager_context() -> str:
    ctx = get_context_manager()
    persona_snapshot = ctx.get("persona_snapshot", {})
    rag_recall = ctx.get("rag_recall", {})
    already_said = ctx.get("already_said", [])
    soul_prompt = ctx.get("soul_prompt", "")

    thoughts = ctx.get("thoughts", [])

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

    context_parts.append(
        "[Image Detection Rule]\n"
        "If the latest user message contains patterns like [image:URL] or [image:https://...], "
        "you MUST create a performer task for visual recognition:\n"
        '  - task_id: "visual_recognition_001"\n'
        '  - description: "Recognize and describe the image at URL: <the_url>. '
        "The user's message context is: <user's text>\\n"
        "Set goto_final_reply=False and populate performer_tasks with this task.\n"
        "Do NOT go to final_reply when there are unprocessed images."
    )

    return "\n\n".join(context_parts)


def _parse_manager_json(raw_text: str) -> ManagerRoute | None:
    if not raw_text or not raw_text.strip():
        return None
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
        return ManagerRoute(**data)
    except (json.JSONDecodeError, Exception) as e:
        logger.debug("Manual JSON parse failed: %s", e)
        return None


def _invoke_manager(messages: list, trimmed: bool = False) -> ManagerRoute | None:
    try:
        result: ManagerRoute = shared_langchain_model.get_structured(ManagerRoute).invoke(messages)
        logger.info("ManagerNode: structured output OK, goto_final=%s, tasks=%d",
                    result.goto_final_reply, len(result.performer_tasks))
        return result
    except Exception:
        logger.warning("ManagerNode: structured output failed (trimmed=%s), trying raw call", trimmed)

    try:
        raw = shared_langchain_model.get().invoke(messages)
        raw_text = raw.content if hasattr(raw, "content") else str(raw)
        logger.warning("ManagerNode: raw LLM output length=%d, first 500 chars: %s",
                       len(raw_text), raw_text[:500])
        parsed = _parse_manager_json(raw_text)
        if parsed:
            logger.info("ManagerNode: manual parse OK, goto_final=%s, tasks=%d",
                        parsed.goto_final_reply, len(parsed.performer_tasks))
            return parsed
    except Exception:
        logger.warning("ManagerNode: raw call also failed", exc_info=True)

    if not trimmed:
        logger.info("ManagerNode: retrying with trimmed messages")
        time.sleep(1)
        return _invoke_manager(messages[-4:], trimmed=True)

    return None


def ManagerNode(
    state: GraphStatus,
) -> Command[Literal["performer", "advance_reply", "final_reply"]]:
    cancel_event = get_cancel_event()
    if cancel_event.is_set():
        logger.info("ManagerNode cancelled, routing to final_reply")
        reply_input = _make_reply_input(
            message="[System: Workflow was cancelled.]",
        )
        return Command(
            goto=[Send("final_reply", reply_input)],
        )

    ctx = get_context_manager()
    ctx.set("thoughts", state.get("thoughts", []))
    ctx.set("already_said", state.get("already_said", []))
    ctx.set("persona_mood", state.get("persona_mood", {}))

    task_results = []
    for cat, items in state.get("tasks_done", {}).items():
        if cat in _REPLY_CATEGORIES:
            continue
        for item in items:
            desc = getattr(item, "description", None) or (
                item.get("description") if isinstance(item, dict) else None
            )
            if desc:
                task_results.append(desc)
    ctx.set("task_results", task_results)

    current_iteration = state.get("iteration_count", 0) + 1
    done_ids = _get_done_ids(state)
    task_count = len(done_ids)

    state_update: dict[str, Any] = {
        "iteration_count": current_iteration,
        "last_task_count": task_count,
    }

    converged, new_counter = _check_convergence(state, task_count)
    state_update["convergence_counter"] = new_counter

    if current_iteration > 1 and converged:
        logger.info(
            "Convergence detected at iteration %d with %d tasks done",
            current_iteration, task_count,
        )
        hint = f"[system: all tasks completed, {task_count} tasks done]"
        tasks = _build_reply_tasks_from_state(state, hint)
        reply_input = _make_reply_input(message=hint, tasks=tasks)
        thoughts_entry = (
            f"[Convergence] No new tasks for {_get_convergence_window()} iterations. "
            f"Total tasks: {task_count}"
        )
        state_update["thoughts"] = [thoughts_entry]
        state_update["convergence_counter"] = 0
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    if current_iteration >= MAX_ITERATIONS:
        tasks = _build_reply_tasks_from_state(state)
        reply_input = _make_reply_input(tasks=tasks)
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    context_str = _assemble_manager_context()
    invoke_messages = [SystemMessage(content=context_str)] + list(state.get("messages", []))
    logger.info("ManagerNode: calling LLM with %d messages", len(invoke_messages))
    result = _invoke_manager(invoke_messages)
    if result is None:
        logger.error("ManagerModel invoke failed after all retries, falling back to final_reply")
        tasks = _build_reply_tasks_from_state(state)
        reply_input = _make_reply_input(tasks=tasks)
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    state_update["thoughts"] = [result.thoughts]
    ctx.set("thoughts", state_update.get("thoughts", []))

    if result.goto_final_reply:
        hint = result.final_reply_hint or ""
        tasks = _build_reply_tasks_from_state(state, hint)
        reply_input = _make_reply_input(message=hint, tasks=tasks)
        return Command(
            update=state_update,
            goto=[Send("final_reply", reply_input)],
        )

    if result.goto_advance_reply:
        hint = result.advance_reply_hint or ""
        tasks = _build_reply_tasks_from_state(state, hint)
        reply_input = _make_reply_input(final=False, message=hint, tasks=tasks)
        return Command(
            update=state_update,
            goto=[Send("advance_reply", reply_input)],
        )

    pending_tasks = [t for t in result.performer_tasks if t.task_id not in done_ids]
    if pending_tasks:
        return Command(
            update=state_update,
            goto=[Send("performer", PerformerInput(task_item=task)) for task in pending_tasks],
        )

    tasks = _build_reply_tasks_from_state(state)
    reply_input = _make_reply_input(tasks=tasks)
    return Command(
        update=state_update,
        goto=[Send("final_reply", reply_input)],
    )
