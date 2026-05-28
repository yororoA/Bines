from typing import Annotated, TypedDict
from operator import add
from langchain.messages import AnyMessage
from .manager_route import TaskItem

_RESET = type("_RESET", (), {"__repr__": lambda self: "_RESET"})()
"""Sentinel value used by custom reducers to clear Annotated fields.

Since LangGraph TypedDict reducer fields cannot be overwritten (they are always
fed through the reducer function), _RESET acts as a special signal:
- merge_tasks: returns empty dict when right is _RESET
- add_str: returns empty string when right is _RESET
- add_list_str: returns empty list when right is _RESET

Usage: return {"field_name": _RESET} from a node to clear that field.
"""


def merge_tasks(left: dict, right: dict) -> dict:
    if right is _RESET:
        return {}
    _left = left or {}
    _right = right or {}
    merged = _left.copy()
    for key, value in _right.items():
        if key in merged:
            existing_ids = {item.task_id for item in merged[key]}
            new_items = [item for item in value if item.task_id not in existing_ids]
            merged[key] = merged[key] + new_items
        else:
            merged[key] = value
    return merged


def add_str(left: str, right: str) -> str:
    if right is _RESET:
        return ""
    return (left or "") + (right or "")


def add_list_str(left: list[str], right: list[str]) -> list[str]:
    if right is _RESET:
        return []
    return (left or []) + (right or [])


class GraphStatus(TypedDict):
    messages: Annotated[list[AnyMessage], add]
    tasks_done: Annotated[dict[str, list[TaskItem]], merge_tasks]
    thoughts: Annotated[str, add_str]
    persona_snapshot: dict
    rag_recall: dict
    already_said: Annotated[list[str], add_list_str]