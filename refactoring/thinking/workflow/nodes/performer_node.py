from utils import generate_sml_model
from thinking_settings import thinking_settings
from tools import webSearch
from status import TaskItem

_performer_model = generate_sml_model(thinking_settings.MODEL_SELECTED)

PerformerAgent = None


def PerformerNode(task_item: TaskItem):
    task_id = task_item.task_id
    task_description = task_item.description

    if PerformerAgent is None:
        PerformerAgent = CodeAgent(
            model=_performer_model,
            tools=[webSearch],
            additional_authorized_imports=["datetime"],
            system_prompt="You are a helpful assistant that can search the web. Always make sure you know the current time.",
            max_tokens=1024,
            max_retries=3,
            max_steps=6,
        )

    result = PerformerAgent.run(task_description)

    return {
        "task_done": {"performer": [TaskItem(task_id=task_id, description=str(result))]}
    }
