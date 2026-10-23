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
    autonomous: bool = False
    contact_allowed: bool = True
    check: object = field(default=None, compare=False)
    journal: object = field(default=None, compare=False)


CURRENT_CONTEXT = ContextVar("bines_tool_context", default=ToolContext())


@contextmanager
def tool_context(context):
    token = CURRENT_CONTEXT.set(context)
    entry = context.journal.begin_turn() if context.journal else None
    try:
        yield context
    except BaseException as exc:
        if entry:
            context.journal.finish(entry, f"{type(exc).__name__}: {exc}", status="failed")
        raise
    else:
        if entry:
            context.journal.finish(entry, getattr(context.journal, "turn_result", ""),
                                   status=getattr(context.journal, "turn_status", "completed"))
    finally:
        CURRENT_CONTEXT.reset(token)
