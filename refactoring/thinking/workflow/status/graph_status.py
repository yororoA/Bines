from typing import Literal
from typing_extensions import Annotated, TypedDict
from operator import add


def merge_tasks(left: dict, right: dict) -> dict:
    """
    left: 当前状态中的字典
    right: 节点返回的新字典
    """
    _left = left if left is not None else {}
    _right = right if right is not None else {}

    merged = _left.copy()
    for key, value in _right.items():
        if key in merged:
            # 如果 Key 已存在，将两个列表合并（相加）
            merged[key] = merged[key] + value
        else:
            # 如果是新 Key，直接赋值
            merged[key] = value
    return merged


class GraphStatus(TypedDict):
    """The status of the graph. Input to the manager route."""

    tasks_demand: Annotated[
        dict[
            Literal["memory_search", "performer", "advance_reply", "final_reply"],
            list[str],
        ],
    ]
    tasks_done: Annotated[
        dict[
            Literal["memory_search", "performer", "advance_reply", "final_reply"],
            list[str],
        ],
        merge_tasks,
    ]
    thoughts: Annotated[str, add]
