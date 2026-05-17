import operator
from typing import Annotated, Optional, Literal, Any

from langchain_core.messages import AnyMessage
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


def _merge_lists(existing: list, new: list) -> list:
    return existing + new


class ThinkingState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], operator.add]
    system_prompt: str
    user_input: str
    source: str
    dynamic_memory_text: str
    dynamic_memory_json: str
    next_node: str
    reasoning: str
    next_purpose: str
    final_reply: Optional[str]
    tool_result: Optional[str]
    tool_history: Annotated[list[dict], _merge_lists]
    state_update_result: Optional[str]
    reply_text: Optional[str]
    tool_round_count: int


class AgentRoute(BaseModel):
    next_node: Literal["tool_agent", "summary_agent", "reply"] = Field(
        description="Route to: tool_agent (real-world operations), summary_agent (state/memory updates), reply (direct dialogue)"
    )
    reasoning: str = Field(description="Brief reasoning for the routing decision")
    next_purpose: str = Field(
        description="If routing to tool_agent: task description. If summary_agent: state update description. If reply: leave empty."
    )
