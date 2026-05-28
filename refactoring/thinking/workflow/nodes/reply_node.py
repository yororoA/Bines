from __future__ import annotations

from tools import send_msg
from smolagents import CodeAgent
from utils import generate_sml_model
from thinking_settings import thinking_settings
from ..status import ReplyInput, TaskItem
from memory import PersonaState

_model = generate_sml_model(thinking_settings.MODEL_SELECTED)


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

    persona_str = (
        f"\n\n[Persona] Tone: {persona.tone}, Style: {persona.style}, "
        f"Role: {persona.role_identity}"
    )

    already_said_str = ""
    if reply_input.already_said:
        already_said_str = (
            "\n\n[Already Said] You have already told the user: "
            + "; ".join(reply_input.already_said[-5:])
            + "\nAvoid repeating these points."
        )

    return base_prompt + persona_str + already_said_str


def ReplyNode(reply_input: ReplyInput) -> dict[str, list[TaskItem]]:
    prompt = _build_reply_system_prompt(reply_input)

    # CodeAgent must be recreated each call because system_prompt carries
    # dynamic persona + already_said from the current ReplyInput.
    agent = CodeAgent(
        model=_model,
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