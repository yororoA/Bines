from tools import send_msg
from smolagents import CodeAgent, tool
from utils import generate_sml_model
from pydantic import BaseModel, Field
from thinking_settings import thinking_settings
from status import ReplyInput, TaskItem


_model = generate_sml_model(thinking_settings.MODEL_SELECTED)


_ReplyAgent = CodeAgent(
    model=_model,
    name="ReplyAgent",
    description="Agent used to reply to the user.",
    tools=[send_msg],
    additional_authorized_imports=["datetime"],
    system_prompt="You are a helpful assistant that can operate QQ or directly reply to the user, your task is to use the tools provided to complete the purpose."
    + "\nAfter you have completed the tasks, you need to return the feedback to the user."
    + "\nThe feedback should be a list of TaskItem objects, each object should have a `task_id` and a `description`:"
    + "\nThe `task_id` is the unique identifier for the task, and the `description` is the feedback of the task.",
    output_schema=list[TaskItem],
    max_tokens=1024,
    max_retries=3,
    max_steps=6,
)


def ReplyNode(reply_input: ReplyInput) -> dict:
    feedback: list[TaskItem] = _ReplyAgent.run(
        {"tasks": reply_input.tasks, "message": reply_input.message}
    )

    return {
        "task_done": {"final_reply" if reply_input.Final else "advance_reply": feedback}
    }
