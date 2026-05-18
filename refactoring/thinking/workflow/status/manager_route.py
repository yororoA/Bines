from pydantic import BaseModel, Field
from typing import Literal, Optional


class ManagerRoute(BaseModel):
    """The route for the manager to follow."""

    next_step: Literal["memory_search", "performer", "advance_reply", "final_reply"] = (
        Field(
            description="The next step in the route."
            + "\nmemory_search: Search the memory for the answer to the question, which the answer may be in the memory."
            + "\nperformer: Perform the task, when you can't find the answer in the memory or need to perform some actions."
            + "\nadvance_reply: Advance the conversation. When you are temporarily unable to answer and need to wait for the execution results returned by `performer` node, and need to give feedback to users in advance."
            + "\nfinal_reply: Final the conversation. When you are able to give the final reply to the user."
        )
    )
    next_step_purpose: str = Field(
        description="The detailed purpose of the next step."
        + "\nFor example, if the next step is memory_search, then the purpose is to search the memory for the answer to the memory."
        + "\nIf the next step is performer, then the purpose is to perform the task."
        + "\nIf the next step is advance_reply, then the purpose is to advance the conversation."
        + "\nIf the next step is final_reply, then the purpose is to final the conversation."
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
