from typing import Annotated, TypedDict
from operator import add
from langchain.messages import AnyMessage
from .manager_route import TaskItem

RESET = type("RESET", (), {"__repr__": lambda self: "RESET"})()

MAX_THOUGHTS = 10
MAX_ITERATIONS = 10
MESSAGE_WINDOW_SIZE = 20
MESSAGE_TRIM_SIZE = 10


def merge_tasks(left: dict, right: dict) -> dict:
    if right is RESET:
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


def cap_list(left: list[str], right: list[str]) -> list[str]:
    if right is RESET:
        return []
    merged = (left or []) + (right or [])
    return merged[-MAX_THOUGHTS:]


def add_list_str(left: list[str], right: list[str]) -> list[str]:
    if right is RESET:
        return []
    return (left or []) + (right or [])


class GraphStatus(TypedDict):
    messages: Annotated[list[AnyMessage], add]
    tasks_done: Annotated[dict[str, list[TaskItem]], merge_tasks]
    thoughts: Annotated[list[str], cap_list]
    iteration_count: int
    last_task_count: int
    convergence_counter: int
    persona_snapshot: dict
    rag_recall: dict
    soul_prompt: str
    persona_mood: dict
    already_said: Annotated[list[str], add_list_str]
    diary_triggered_day: str
    invocation_count: int
    thread_id: str