from typing import Annotated, TypedDict
from operator import add
from .manager_route import TaskItem


def merge_tasks(left: dict, right: dict) -> dict:
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


class GraphStatus(TypedDict):
    tasks_demand: Annotated[dict[str, list[TaskItem]], merge_tasks]
    tasks_done: Annotated[dict[str, list[TaskItem]], merge_tasks]
    thoughts: Annotated[str, add]
