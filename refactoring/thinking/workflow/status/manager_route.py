from pydantic import BaseModel, Field
from typing import Literal, Optional


class TaskItem(BaseModel):
    task_id: str = Field(
        description="Unique identifier for the task, e.g., 'weather_001'"
    )
    description: str = Field(description="Detailed purpose of the task")


class ManagerRoute(BaseModel):
    """The route for the manager to output."""

    tasks_demand: dict[
        Literal["memory_search", "performer", "advance_reply", "final_reply"],
        list[TaskItem],
    ] = Field(
        title="The tasks need to be performed",
        description=(
            "A dictionary where keys are task types and values are lists of TaskItem objects. "
            "Each TaskItem MUST have a unique 'task_id' (e.g., 'search_001', 'ws_api_ref_23') "
            "and a 'description'. The 'task_id' is used to track progress and prevent "
            "duplicate execution in loop cycles. Before adding a new task, check if a "
            "similar task ID already exists in 'tasks_done'."
        ),
        examples=[
            {
                "memory_search": [
                    {
                        "task_id": "mem_query_birthday",
                        "description": "Find out if there are birthday related answers in the memory."
                    },
                    {
                        "task_id": "mem_query_hobby",
                        "description": "Find out the hobby in the memory."
                    }
                ],
                "performer": [
                    {
                        "task_id": "web_search_president",
                        "description": "Search the internet for the current president of the United States."
                    }
                ],
                "advance_reply": [
                    {
                        "task_id": "thinking_msg_01",
                        "description": "Reply the user in advance with `Um, let me think...`"
                    }
                ],
                "final_reply": [
                    {
                        "task_id": "final_msg_01",
                        "description": "Reply the user with the final words `The weather of New York is sunny now.`"
                    }
                ],
            }
        ],
    )
    thoughts: str = Field(
        title="The thoughts of your current decision",
        description="The thoughts of your current decision."
        + "\nYou can fill this field with the thoughts about the answer to the question, or the tasks to perform."
        + "\nFor example, the user asked a question which you need to search the memory or perform some actions for the answer, then you can fill this field with the thoughts about why you need to search the memory or perform some actions."
        + "\nAnd after the task is performed, you can continue to think about the next step according to the task results and the past thoughts in the this field.",
    )
    advance_reply: Optional[str] = Field(
        default=None,
        description="The reply to advance the conversation. Only used when the next step is advance_reply."
        "\nFor example, you can't answer the question immediately, and need to wait for the feedback from `performer` node, and need to give feedback to users in advance."
        + "\nYou can use this field to give feedback to users in advance.",
    )
    final_reply: Optional[str] = Field(
        default=None,
        description="The reply to final the conversation. Only used when the next step is final_reply."
        "\nFor example, you can answer the question immediately, and need to give the final reply to the user."
        + "\nYou can use this field to give the final reply to the user.",
    )
