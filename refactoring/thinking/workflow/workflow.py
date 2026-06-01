import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain.messages import HumanMessage

logger = logging.getLogger(__name__)

from .status import GraphStatus, MAX_ITERATIONS
from .cancel import get_cancel_event
from .context_manager import get_context_manager
from .nodes import (
    PerformerNode,
    ContextBuilderNode,
    StatusTrimNode,
    DynamicAgentNode,
)

_cancel_event = get_cancel_event()


class Workflow:
    def __init__(self):
        self._sqlite_conn: sqlite3.Connection | None = None
        self._checkpointer: SqliteSaver | None = None
        self._app = self._compile()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workflow")

    @staticmethod
    def _patch_msgpack_serializer():
        import ormsgpack
        from langgraph.checkpoint.serde.jsonplus import _msgpack_default as _orig_default

        def _patched_default(obj):
            try:
                return _orig_default(obj)
            except TypeError:
                return repr(obj)

        import langgraph.checkpoint.serde.jsonplus as m
        m._msgpack_default = _patched_default
        m._msgpack_enc = lambda data: ormsgpack.packb(
            data, default=_patched_default,
            option=getattr(m, "_option", None),
        )

    def _build_Workflow(self):
        workflow = StateGraph(GraphStatus)

        workflow.add_node(
            "context_builder",
            ContextBuilderNode,
            description="Builds conversational context from STM, RAG, and persona.",
        )
        workflow.add_node(
            "performer",
            PerformerNode,
            description="The autonomous agent that handles all user requests end-to-end.",
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

        workflow.add_edge("context_builder", "performer")
        workflow.add_edge("performer", "dynamic_agent")
        workflow.add_edge("dynamic_agent", "status_trim")
        workflow.add_edge("status_trim", END)

        return workflow

    def _compile(self):
        self._patch_msgpack_serializer()
        base_dir = Path(__file__).resolve().parents[1]
        checkpoints_dir = base_dir / "memory_data/checkpoints"
        checkpoints_dir.mkdir(exist_ok=True, parents=True)

        self._sqlite_conn = sqlite3.connect(
            checkpoints_dir / "checkpoints.db", check_same_thread=False, timeout=30
        )
        self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
        memory = SqliteSaver(self._sqlite_conn)
        self._checkpointer = memory

        return self._build_Workflow().compile(checkpointer=memory)

    def _prune_checkpoints(self, thread_id: str) -> None:
        if not self._sqlite_conn:
            logger.warning("Prune: sqlite_conn is None, skipping")
            return
        try:
            cur = self._sqlite_conn.cursor()
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM checkpoints "
                    "WHERE thread_id = ? AND checkpoint_ns = ''",
                    (thread_id,),
                )
                total = cur.fetchone()[0]
                logger.info("Prune: found %d checkpoints for thread=%s", total, thread_id)
                if total <= 1:
                    return

                cur.execute(
                    "SELECT checkpoint_id FROM checkpoints "
                    "WHERE thread_id = ? AND checkpoint_ns = '' "
                    "ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id,),
                )
                keep = cur.fetchone()
                if not keep:
                    return
                keep_id = keep[0]

                cur.execute(
                    "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = '' "
                    "AND checkpoint_id != ?",
                    (thread_id, keep_id),
                )
                cur.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' "
                    "AND checkpoint_id != ?",
                    (thread_id, keep_id),
                )
                self._sqlite_conn.commit()
                deleted = total - 1
                logger.info(
                    "Pruned %d old checkpoints for thread=%s, kept=%s",
                    deleted, thread_id, keep_id,
                )
            finally:
                cur.close()
        except Exception:
            logger.warning(
                "Failed to prune checkpoints for thread=%s", thread_id, exc_info=True
            )

    def invoke(self, input: str, thread_id: str, timeout: float | None = None, cancel_event: threading.Event | None = None):
        from thinking_settings import thinking_settings

        if timeout is None:
            timeout = thinking_settings.WORKFLOW_TIMEOUT_SECONDS

        active_cancel_event = cancel_event if cancel_event is not None else _cancel_event

        logger.info("Workflow.invoke: thread=%s, input=%s, timeout=%.1fs", thread_id, input[:100], timeout)
        initial_state = GraphStatus(
            messages=[HumanMessage(content=input)],
            thread_id=thread_id,
            cancel_event=active_cancel_event,
        )
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": MAX_ITERATIONS * 4 + 10,
        }

        active_cancel_event.clear()

        def _run():
            get_context_manager().reset()
            return self._app.invoke(initial_state, config)

        future = self._executor.submit(_run)
        try:
            result = future.result(timeout=timeout)
            logger.info("Workflow.invoke: completed for thread=%s", thread_id)
            self._prune_checkpoints(thread_id)
            return result
        except FuturesTimeoutError:
            logger.error(
                "Workflow timeout after %.1fs for thread %s. "
                "The task may still be running in background.",
                timeout, thread_id,
            )
            active_cancel_event.set()
            future.cancel()
            return {
                "messages": [HumanMessage(content="[System: Workflow timed out. Please try again.]")],
            }

    def close(self):
        _cancel_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self._sqlite_conn:
            try:
                self._sqlite_conn.close()
            except Exception:
                logger.warning("Failed to close SQLite connection", exc_info=True)
            self._sqlite_conn = None
        logger.info("Workflow resources released")

    def inject_message(self, content: str, thread_id: str) -> None:
        try:
            self._app.update_state(
                config={"configurable": {"thread_id": thread_id}},
                values={"messages": [HumanMessage(content=content)]},
            )
        except Exception:
            logger.warning(
                "Failed to inject message into thread %s", thread_id, exc_info=True
            )