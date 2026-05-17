from langgraph.graph import StateGraph, END
from .nodes import manager_node, reply_node, web_search_node, rag_search_node
from .messagesState import MessagesState


def jump(state: MessagesState):
    return state["next_node"]


workflow = StateGraph(MessagesState)

workflow.add_node("manager", manager_node)
workflow.add_node("reply", reply_node)
workflow.add_node("web_search", web_search_node)
workflow.add_node("rag_search", rag_search_node)

workflow.set_entry_point("manager")

workflow.add_conditional_edges(
    "manager",
    jump,
    {
        "web_search": "web_search",
        "rag_search": "rag_search",
        "reply": "reply",
    },
)
workflow.add_edge("web_search", "manager")
workflow.add_edge("rag_search", "manager")

workflow.add_edge("reply", END)

app = workflow.compile()
