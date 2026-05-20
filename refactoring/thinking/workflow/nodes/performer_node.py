from utils import generate_sml_model
from thinking_settings import thinking_settings
from tools import webSearch

_performer_model = generate_sml_model(thinking_settings.MODEL_SELECTED)

PerformerAgent = None

def PerformerNode(task_description: str):
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
      "task_done": [f"The task `[{task_description}]` is done. The result is `[{result}]`"]
    }