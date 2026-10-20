"""Immutable per-turn tool authority. Models cannot supply or overwrite it."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from chatBot.messages import LOCAL_IDENTITY, Identity


@dataclass(frozen=True)
class ToolContext:
    identity: Identity = LOCAL_IDENTITY
    event_id: str | None = None
    turn_id: str | None = None
    receipts: list = field(default_factory=list, compare=False)


CURRENT_CONTEXT = ContextVar("bines_tool_context", default=ToolContext())


@contextmanager
def tool_context(context):
    token = CURRENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        CURRENT_CONTEXT.reset(token)
