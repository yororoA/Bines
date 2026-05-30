import hashlib
import logging
import threading

from utils import shared_smol_model
from smolagents import CodeAgent
from tools import get_tool_registry, PERFORMER_TOOLS
from ..status import PerformerInput, TaskItem

logger = logging.getLogger(__name__)

_PerformerAgent = None
_PerformerLock = threading.Lock()
_PerformerRunLock = threading.Lock()
_cached_soul_hash: str | None = None


def _soul_hash(soul: str) -> str:
    return hashlib.md5(soul.encode()).hexdigest()[:16]


def PerformerNode(performer_input: PerformerInput) -> dict[str, list[TaskItem]]:
    global _PerformerAgent, _cached_soul_hash

    task_item = performer_input.task_item
    task_id = task_item.task_id
    task_description = task_item.description
    soul_prompt = performer_input.soul_prompt or ""
    current_hash = _soul_hash(soul_prompt)

    try:
        needs_rebuild = _PerformerAgent is None or current_hash != _cached_soul_hash
        if needs_rebuild:
            with _PerformerLock:
                if _PerformerAgent is None or current_hash != _cached_soul_hash:
                    registry = get_tool_registry()
                    tools = registry.get_tools(PERFORMER_TOOLS)
                    imports = registry.get_authorized_imports(PERFORMER_TOOLS)

                    base_prompt = (
                        "You are a helpful assistant that can search the web. "
                        "Always make sure you know the current time."
                    )
                    system_prompt = f"{soul_prompt}\n\n{base_prompt}" if soul_prompt else base_prompt

                    _PerformerAgent = CodeAgent(
                        model=shared_smol_model.get(),
                        tools=tools,
                        additional_authorized_imports=["datetime", *imports],
                        system_prompt=system_prompt,
                        max_tokens=1024,
                        max_retries=3,
                        max_steps=6,
                    )
                    _cached_soul_hash = current_hash

        with _PerformerRunLock:
            result = _PerformerAgent.run(task_description)

        return {
            "tasks_done": {"performer": [TaskItem(task_id=task_id, description=str(result))]}
        }
    except Exception as e:
        logger.exception("PerformerNode failed for task %s", task_id)
        return {
            "tasks_done": {"performer": [TaskItem(
                task_id=task_id,
                description=f"[TASK_FAILED] The search task encountered an error and could not be completed. Task: {task_description[:100]}",
            )]}
        }