from __future__ import annotations

import hashlib
import logging
import threading
import uuid

from smolagents import CodeAgent
from utils import shared_smol_model
from ..status import ReplyInput, TaskItem
from ..cancel import get_cancel_event
from memory import PersonaState, retrieve_for_reply, format_retrieval_results
from tools import get_tool_registry, REPLY_TOOLS

logger = logging.getLogger(__name__)

_ReplyAgent = None
_ReplyTools = None
_ReplyLock = threading.Lock()
_ReplyRunLock = threading.Lock()
_cached_prompt_hash: str | None = None


def _prompt_hash(prompt: str) -> str:
    return hashlib.md5(prompt.encode()).hexdigest()[:16]


def _build_reply_system_prompt(reply_input: ReplyInput) -> str:
    persona = PersonaState.from_dict(reply_input.persona_snapshot)
    base_prompt = (
        "You are a helpful assistant that can operate QQ or directly reply to the user."
        "\nYour task is to use the tools provided to complete the purpose."
        "\nAfter you have completed the tasks, you need to return the feedback to the user."
        "\nThe feedback should be a list of TaskItem objects, "
        "each object should have a `task_id` and a `description`:"
        "\nThe `task_id` is the unique identifier for the task, "
        "and the `description` is the feedback of the task."
    )

    parts = [base_prompt]

    if reply_input.soul_prompt:
        parts.append(reply_input.soul_prompt)

    profile_str = persona.to_prompt_string()
    if profile_str:
        parts.append(f"\n\n{profile_str}")

    rag = reply_input.rag_recall
    if rag and rag.get("formatted"):
        parts.append(f"\n\n[RAG Context]\n{rag['formatted']}")
    elif reply_input.message:
        results = retrieve_for_reply(reply_input.message)
        formatted = format_retrieval_results(results)
        if formatted:
            parts.append(f"\n\n[RAG Context]\n{formatted}")

    if reply_input.already_said:
        already_said_str = (
            "\n\n[Already Said] You have already told the user: "
            + "; ".join(reply_input.already_said[-5:])
            + "\nAvoid repeating these points."
        )
        parts.append(already_said_str)

    return "\n".join(parts)


def ReplyNode(reply_input: ReplyInput) -> dict[str, list[TaskItem]]:
    global _ReplyAgent, _ReplyTools, _cached_prompt_hash

    try:
        cancel_event = get_cancel_event()
        if cancel_event.is_set():
            logger.info("ReplyNode cancelled")
            fallback = [TaskItem(task_id=f"reply_cancel_{uuid.uuid4().hex[:8]}", description="[CANCELLED] Workflow was cancelled due to timeout.")]
            return {
                "tasks_done": {"final_reply" if reply_input.Final else "advance_reply": fallback},
                "already_said": [],
            }

        prompt = _build_reply_system_prompt(reply_input)
        current_hash = _prompt_hash(prompt)

        needs_rebuild = _ReplyAgent is None or current_hash != _cached_prompt_hash
        if needs_rebuild:
            with _ReplyLock:
                if _ReplyAgent is None or current_hash != _cached_prompt_hash:
                    if _ReplyTools is None:
                        registry = get_tool_registry()
                        _ReplyTools = registry.get_tools(REPLY_TOOLS)

                    _ReplyAgent = CodeAgent(
                        model=shared_smol_model.get(),
                        name="ReplyAgent",
                        description="Agent used to reply to the user.",
                        tools=_ReplyTools,
                        additional_authorized_imports=["datetime"],
                        system_prompt=prompt,
                        output_schema=list[TaskItem],
                        max_tokens=1024,
                        max_retries=3,
                        max_steps=6,
                    )
                    _cached_prompt_hash = current_hash

        with _ReplyRunLock:
            feedback: list[TaskItem] = _ReplyAgent.run(
                {"tasks": reply_input.tasks, "message": reply_input.message}
            )

        already_said_entries = [item.description for item in feedback if item.description]

        return {
            "tasks_done": {"final_reply" if reply_input.Final else "advance_reply": feedback},
            "already_said": already_said_entries,
        }
    except Exception:
        logger.exception("ReplyNode failed")
        fallback = [TaskItem(task_id=f"reply_error_{uuid.uuid4().hex[:8]}", description="[REPLY_FAILED] Could not generate a reply. Please try rephrasing your message.")]
        return {
            "tasks_done": {"final_reply" if reply_input.Final else "advance_reply": fallback},
            "already_said": [],
        }