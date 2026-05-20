from tools import send_msg
from smolagents import CodeAgent
from utils import generate_sml_model
from thinking_settings import thinking_settings


_model = generate_sml_model(thinking_settings.MODEL_SELECTED)


NapCatAgent = CodeAgent(
    model=_model,
    name="NapCatAgent",
    description="Agent used to send QQ messages.",
    tools=[send_msg],
    additional_authorized_imports=[],
    system_prompt="You are a helpful assistant that can operate QQ, your task is to use the tools provided to complete the purpose.",
    max_tokens=1024,
    max_retries=3,
    max_steps=6,
)

ReplyAgent = CodeAgent(
    model=_model,
    name="ReplyAgent",
    description="Agent used to reply to the user.",
    managed_agents=[NapCatAgent],
    planning_interval=4,
    verbosity_level=1,
    max_steps=6,
)

def ReplyNode()
