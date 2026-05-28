import logging
import threading

from utils import shared_smol_model
from smolagents import CodeAgent
from tools import get_tool_registry, PERFORMER_TOOLS
from ..status import PerformerInput, TaskItem

logger = logging.getLogger(__name__)

_PerformerAgent = None
_PerformerLock = threading.Lock()


def PerformerNode(performer_input: PerformerInput) -> dict[str, list[TaskItem]]:
    global _PerformerAgent

    task_item = performer_input.task_item
    task_id = task_item.task_id
    task_description = task_item.description
    soul_prompt = performer_input.soul_prompt or ""

    try:
        if _PerformerAgent is None:
            with _PerformerLock:
                if _PerformerAgent is None:
                    registry = get_tool_registry()
                    tools = registry.get_tools(PERFORMER_TOOLS)
                    imports = registry.get_authorized_imports(PERFORMER_TOOLS)

                    system_prompt = (
                        "You are a helpful assistant that can search the web. "
                        "Always make sure you know the current time."
                    )
                    if soul_prompt:
                        system_prompt = soul_prompt + "\n\n" + system_prompt

                    _PerformerAgent = CodeAgent(
                        model=shared_smol_model.get(),
                        tools=tools,
                        additional_authorized_imports=["datetime", *imports],
                        system_prompt=system_prompt,
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