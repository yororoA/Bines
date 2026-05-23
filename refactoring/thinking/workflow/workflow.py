from thinking_settings import thinking_settings
from langgraph.graph import StateGraph, START, END
from .status import GraphStatus, ManagerRoute
from .nodes import ManagerNode, PerformerNode, ReplyNode, MemorySearchNode
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
from typing import Literal
from pathlib import Path


class Workflow:
    def __init__(self, thread_id: Literal["raw_chat", "QQ"]):
        self.workflow = self._build_Workflow()
        self.checkpoint_saver = SqliteSaver(f"checkpoint_{thread_id}.db")

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

        workflow.set_entry_point("context_builder")

        workflow.add_edge("context_builder", "manager")
        workflow.add_edge("memory_search", "manager")
        workflow.add_edge("performer", "manager")
        workflow.add_edge("advance_reply", "manager")
        workflow.add_edge("final_reply", END)

        return workflow

    def compile(self):
        base_dir = Path(__file__).resolve().parents[2]
        data_dir = base_dir / "data/checkpoints"
        data_dir.mkdir(exist_ok=True)

        conn = sqlite3.connect(data_dir / "checkpoints.db")

        return self.workflow.compile()
