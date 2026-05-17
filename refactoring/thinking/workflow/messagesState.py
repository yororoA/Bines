from langchain.messages import AnyMessage
from typing import TypedDict, Annotated, Literal, Optional
import operator
from pydantic import BaseModel, Field


class AgentRoute(BaseModel):
    next_node: Literal["web_search", "rag_search", "reply"] = Field(
        ...,
        description="The next node to route to after the agent finishes its task. It can be 'web_search', 'rag_search', or 'reply'.",
    )
    reasoning: str = Field(..., description="The reasoning behind the decision.")
    next_purpose: str = Field(
        ...,
        description="The detailed next purpose or goal that the next node should aim for.",
    )
    final_reply: Optional[str] = Field(
        None,
        description="The final reply to the user. Must be used when next_node is 'reply'.",
    )


class MessagesState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    thinking: Annotated[list[str], operator.add]
    purposed_nodes: Annotated[
        list[str], operator.add
    ]  # the nodes that have been purposed in the past, used for manager node to decide whether to route to a node or not. For example, if web_search has been purposed before, then manager node can decide to not route to web_search again to avoid infinite loop.
    purposed_feedbacks: Annotated[
        list[str], operator.add
    ]  # the feedbacks for the purposed nodes, used for manager node to adjust the routing decision. For example, if the feedback for web_search is always negative, then manager node can decide to not route to web_search again.
    next_node: str
    reasoning: str
    next_purpose: str
    final_reply: Optional[str]
