import logging

from utils import shared_smol_model
from smolagents import CodeAgent
from tools import webSearch
from tools.performer_tools.webSearch import get_search_tools, SEARCH_AUTHORIZED_IMPORTS
from ..status import TaskItem

logger = logging.getLogger(__name__)

_PerformerAgent = None


def PerformerNode(task_item: TaskItem) -> dict[str, list[TaskItem]]:
    global _PerformerAgent

    task_id = task_item.task_id
    task_description = task_item.description

    try:
        if _PerformerAgent is None:
            _PerformerAgent = CodeAgent(
                model=shared_smol_model.get(),
                tools=[webSearch, *get_search_tools()],
                additional_authorized_imports=["datetime", *SEARCH_AUTHORIZED_IMPORTS],
                system_prompt="You are a helpful assistant that can search the web. Always make sure you know the current time.",
                max_tokens=1024,
                max_retries=3,
                max_steps=6,
            )

        result = _PerformerAgent.run(task_description)

        return {
            "tasks_done": {"performer": [TaskItem(task_id=task_id, description=str(result))]}
        }
    except Exception as e:
        logger.exception("PerformerNode failed for task %s", task_id)
        return {
            "tasks_done": {"performer": [TaskItem(
                task_id=task_id,
                description=f"[Error] Task failed: {e}",
            )]}
        }