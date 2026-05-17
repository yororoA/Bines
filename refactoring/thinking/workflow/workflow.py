from langgraph.graph import StateGraph, END

from workflow.state import ThinkingState
from workflow.nodes import (
    context_builder_node,
    manager_node,
    reply_node,
    tool_agent_node,
    summary_agent_node,
)


def _route_after_manager(state: dict) -> str:
    next_node = state.get("next_node", "reply")
    if next_node == "tool_agent":
        return "tool_agent"
    elif next_node == "summary_agent":
        return "summary_agent"
    else:
        return "reply"


def build_workflow() -> StateGraph:
    graph = StateGraph(ThinkingState)

    graph.add_node("context_builder", context_builder_node)
    graph.add_node("manager", manager_node)
    graph.add_node("tool_agent", tool_agent_node)
    graph.add_node("summary_agent", summary_agent_node)
    graph.add_node("reply", reply_node)

    graph.set_entry_point("context_builder")
    graph.add_edge("context_builder", "manager")

    graph.add_conditional_edges(
        "manager",
        _route_after_manager,
        {
            "tool_agent": "tool_agent",
            "summary_agent": "summary_agent",
            "reply": "reply",
        },
    )

    graph.add_edge("tool_agent", "manager")
    graph.add_edge("summary_agent", "manager")
    graph.add_edge("reply", END)

    return graph


app = build_workflow().compile()
