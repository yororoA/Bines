from thinking_settings import thinking_settings
from langgraph.graph import StateGraph, END
from .status import GraphStatus
from .nodes import ManagerNode, PerformerNode, ReplyNode, MemorySearchNode
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
from pathlib import Path
from status import GraphStatus
from typing import Literal
from langchain.messages import HumanMessage


class Workflow:
    def __init__(self):
        self._app = self._compile()

    def _build_Workflow(self):
        workflow = StateGraph(GraphStatus)

        workflow.add_node(
            ManagerNode,
            name="manager",
            description="The manager node responsible for task planning and coordination.",
        )
        workflow.add_node(
            MemorySearchNode,
            name="memory_search",
            description="The memory search node responsible for searching memory for relevant information.",
        )
        workflow.add_node(
            PerformerNode,
            name="performer",
            description="The performer node responsible for executing tasks assigned by the manager.",
        )
        workflow.add_node(
            ReplyNode,
            name="advance_reply",
            description="The reply node responsible for generating responses to user queries.",
        )
        workflow.add_node(
            ReplyNode,
            name="final_reply",
            description="The reply node responsible for generating final responses to user queries.",
        )
        # todo: add context_builder node, connect with memory_search_node
        # context -> manager -> memory_search ---(search result)--> context -> manager
        workflow.set_entry_point("context_builder")

        workflow.add_edge("context_builder", "manager")
        workflow.add_edge("memory_search", "manager")
        workflow.add_edge("performer", "manager")
        workflow.add_edge("advance_reply", "manager")
        workflow.add_edge("final_reply", END)

        return workflow

    def _compile(self):
        if self._app is not None:
            return self._app

        base_dir = Path(__file__).resolve().parents[2]
        checkpoints_dir = base_dir / "data/checkpoints"
        checkpoints_dir.mkdir(exist_ok=True, parents=True)

        conn = sqlite3.connect(
            checkpoints_dir / "checkpoints.db", check_same_thread=False
        )
        memory = SqliteSaver(conn)

        return self._build_Workflow().compile(checkpointer=memory)

    def invoke(self, input: str, thread_id: Literal["raw_chat", "QQ"]):
        initial_state = GraphStatus(messages=[HumanMessage(content=input)])
        return self._app.invoke(
            initial_state, config={"configurable": {"thread_id": thread_id}}
        )
