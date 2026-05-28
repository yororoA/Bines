import sqlite3
from pathlib import Path
from typing import Literal

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain.messages import HumanMessage

from .status import GraphStatus
from .nodes import (
    ManagerNode,
    PerformerNode,
    ReplyNode,
    ContextBuilderNode,
    StatusTrimNode,
    DynamicAgentNode,
)


class Workflow:
    def __init__(self):
        self._app = self._compile()

    def _build_Workflow(self):
        workflow = StateGraph(GraphStatus)

        workflow.add_node(
            "context_builder",
            ContextBuilderNode,
            description="Builds conversational context from STM, RAG, and persona.",
        )
        workflow.add_node(
            "manager",
            ManagerNode,
            description="The manager node responsible for task planning and advance_reply decisions.",
        )
        workflow.add_node(
            "performer",
            PerformerNode,
            description="The performer node responsible for executing tasks assigned by the manager.",
        )
        workflow.add_node(
            "advance_reply",
            ReplyNode,
            description="Generates intermediate replies to keep the user informed during task execution.",
        )
        workflow.add_node(
            "final_reply",
            ReplyNode,
            description="Generates the final reply to the user after all tasks are completed.",
        )
        workflow.add_node(
            "status_trim",
            StatusTrimNode,
            description="Trims graph status to only keep messages, clearing ephemeral state.",
        )
        workflow.add_node(
            "dynamic_agent",
            DynamicAgentNode,
            description="Updates persona memory and handles STM overflow after final reply.",
        )

        workflow.set_entry_point("context_builder")

        workflow.add_edge("context_builder", "manager")
        workflow.add_edge("performer", "manager")
        workflow.add_edge("advance_reply", "manager")
        workflow.add_edge("final_reply", "dynamic_agent")
        workflow.add_edge("dynamic_agent", "status_trim")
        workflow.add_edge("status_trim", END)

        return workflow

    def _compile(self):
        base_dir = Path(__file__).resolve().parents[2]
        checkpoints_dir = base_dir / "data/checkpoints"
        checkpoints_dir.mkdir(exist_ok=True, parents=True)

        conn = sqlite3.connect(
            checkpoints_dir / "checkpoints.db", check_same_thread=False
        )
        memory = SqliteSaver(conn)

        return self._build_Workflow().compile(checkpointer=memory)

    def invoke(
        self,
        input: str,
        thread_id: Literal["raw_chat", "QQ_private", "QQ_group"],
    ):
        initial_state = GraphStatus(messages=[HumanMessage(content=input)])
        return self._app.invoke(
            initial_state,
            config={"configurable": {"thread_id": thread_id}},
        )