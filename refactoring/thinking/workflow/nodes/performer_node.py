import hashlib
import logging
import threading

from utils import shared_smol_model
from smolagents import CodeAgent
from tools import get_tool_registry, PERFORMER_TOOLS
from ..status import PerformerInput, TaskItem
from ..cancel import get_cancel_event

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
        cancel_event = get_cancel_event()
        if cancel_event.is_set():
            logger.info("PerformerNode cancelled for task %s", task_id)
            return {
                "tasks_done": {"performer": [TaskItem(
                    task_id=task_id,
                    description="[CANCELLED] Task was cancelled due to workflow timeout.",
                )]}
            }

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

                    import copy

                    from smolagents.agents import EMPTY_PROMPT_TEMPLATES

                    _PerformerAgent = CodeAgent(
                        model=shared_smol_model.get(),
                        tools=tools,
                        additional_authorized_imports=["datetime", *imports],
                        prompt_templates={**copy.deepcopy(EMPTY_PROMPT_TEMPLATES), "system_prompt": system_prompt},
                        max_steps=6,
                    )
                    _cached_soul_hash = current_hash

        logger.info("PerformerNode: running task %s: %s", task_id, task_description[:100])
        with _PerformerRunLock:
            result = _PerformerAgent.run(task_description)
        logger.info("PerformerNode: task %s completed, result=%s", task_id, str(result)[:200])

        return {
            "tasks_done": {"performer": [TaskItem(task_id=task_id, description=str(result))]}
        }
    except Exception:
        logger.exception("PerformerNode failed for task %s", task_id)
        return {
            "tasks_done": {"performer": [TaskItem(
                task_id=task_id,
                description=f"[TASK_FAILED] The search task encountered an error and could not be completed. Task: {task_description[:100]}",
            )]}
        }