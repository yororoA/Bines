from pydantic import BaseModel, Field
from typing import Literal, Optional


class ManagerRoute(BaseModel):
    """The route for the manager to output."""

    tasks_demand: dict[
        Literal["memory_search", "performer", "advance_reply", "final_reply"], list[str]
    ] = Field(
        title="The tasks need to be performed",
        description="key: the task name, value: a list of the detailed task purpose descriptions",
        examples=[
            {
                "memory_search": ["Find out if there are birthday related answers in the memory."],
                "performer": ["Search the internet for the current president of the United States."],   
                "advance_reply": ["Reply the user in advance with `Um, let me think...`"],
                "final_reply": ["Reply the user with the final words `The weather of New York is sunny now.`"], 
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
