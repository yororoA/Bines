from __future__ import annotations

import logging
import threading

from smolagents import CodeAgent
from utils import shared_smol_model
from ..status import ReplyInput, TaskItem
from memory import PersonaState, retrieve_for_reply, format_retrieval_results
from tools import get_tool_registry, REPLY_TOOLS

logger = logging.getLogger(__name__)

_ReplyModel = None
_ReplyTools = None
_ReplyLock = threading.Lock()


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

    profile_parts = []
    if persona.user_name:
        profile_parts.append(f"Name: {persona.user_name}")
    if persona.speaking_habits:
        profile_parts.append(f"Speaking habits: {'; '.join(persona.speaking_habits)}")
    if persona.long_term_preferences:
        profile_parts.append(f"Preferences: {'; '.join(persona.long_term_preferences)}")
    if persona.tech_stack:
        profile_parts.append(f"Tech stack: {'; '.join(persona.tech_stack)}")
    if persona.user_preferences:
        profile_parts.append(f"User preferences: {'; '.join(persona.user_preferences)}")
    persona_str = ""
    if profile_parts:
        persona_str = "\n\n[User Profile]\n" + "\n".join(profile_parts)
    if persona_str:
        parts.append(persona_str)

    if reply_input.message:
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
    global _ReplyModel, _ReplyTools

    try:
        prompt = _build_reply_system_prompt(reply_input)

        if _ReplyModel is None:
            with _ReplyLock:
                if _ReplyModel is None:
                    _ReplyModel = shared_smol_model.get()

        if _ReplyTools is None:
            with _ReplyLock:
                if _ReplyTools is None:
                    registry = get_tool_registry()
                    _ReplyTools = registry.get_tools(REPLY_TOOLS)

        agent = CodeAgent(
            model=_ReplyModel,
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

        feedback: list[TaskItem] = agent.run(
            {"tasks": reply_input.tasks, "message": reply_input.message}
        )

        already_said_entries = [item.description for item in feedback if item.description]

        return {
            "tasks_done": {"final_reply" if reply_input.Final else "advance_reply": feedback},
            "already_said": already_said_entries,
        }
    except Exception as e:
        logger.exception("ReplyNode failed")
        fallback = [TaskItem(task_id="reply_error", description=str(reply_input.message or ""))]
        return {
            "tasks_done": {"final_reply" if reply_input.Final else "advance_reply": fallback},
            "already_said": [],
        }