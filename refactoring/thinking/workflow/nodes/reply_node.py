from __future__ import annotations

import logging

from tools import send_msg
from smolagents import CodeAgent
from utils import shared_smol_model
from ..status import ReplyInput, TaskItem
from memory import PersonaState, retrieve_for_reply, format_retrieval_results

logger = logging.getLogger(__name__)


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

    persona_str = (
        f"\n\n[Persona] Tone: {persona.tone}, Style: {persona.style}, "
        f"Role: {persona.role_identity}"
    )
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
    try:
        prompt = _build_reply_system_prompt(reply_input)

        agent = CodeAgent(
            model=shared_smol_model.get(),
            name="ReplyAgent",
            description="Agent used to reply to the user.",
            tools=[send_msg],
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