from smolagents import OpenAIModel
from langchain.chat_models import init_chat_model
from thinking_settings import thinking_settings
from typing import Literal

ModelList = Literal[
    "deepseek-v4-flash", "deepseek-v4-pro", "mimo-v2.5", "mimo-v2.5-pro"
]


def smolagents_model(
    *,
    model: ModelList = "deepseek-v4-flash",
    **kwargs,
):
    if model in ["deepseek-v4-flash", "deepseek-v4-pro"]:
        return OpenAIModel(
            model=model,
            api_url=thinking_settings.DEEPSEEK_API_URL,
            api_key=thinking_settings.DEEPSEEK_API_KEY,
            **kwargs,
        )
    elif model in ["mimo-v2.5", "mimo-v2.5-pro"]:
        return OpenAIModel(
            model=model,
            api_url=thinking_settings.MIMO_API_URL,
            api_key=thinking_settings.MIMO_API_KEY,
            **kwargs,
        )
    else:
        raise ValueError(
            f"Unsupported model: {model}\nSupported models: {thinking_settings.MODEL_LIST}"
        )


def langchain_model(
    model: ModelList = "deepseek-v4-flash",
    **kwargs,
):
    if model in ["deepseek-v4-flash", "deepseek-v4-pro"]:
        return init_chat_model(
            model=model,
            api_url=thinking_settings.DEEPSEEK_API_URL,
            api_key=thinking_settings.DEEPSEEK_API_KEY,
            **kwargs,
        )
    elif model in ["mimo-v2.5", "mimo-v2.5-pro"]:
        return init_chat_model(
            model=model,
            api_url=thinking_settings.MIMO_API_URL,
            api_key=thinking_settings.MIMO_API_KEY,
            **kwargs,
        )
    else:
        raise ValueError(
            f"Unsupported model: {model}\nSupported models: {thinking_settings.MODEL_LIST}"
        )
