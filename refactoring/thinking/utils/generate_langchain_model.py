from langchain.chat_models import init_chat_model, BaseChatModel

from .model_registry import get_model_registry, register_default_providers

_initialized = False


def _init_registry():
    global _initialized
    if not _initialized:
        register_default_providers()
        _initialized = True


def generate_langchain_model(model_name: str) -> BaseChatModel:
    _init_registry()
    registry = get_model_registry()
    provider = registry.find(model_name)
    if not provider:
        default = registry.get_default()
        if default:
            return init_chat_model(
                model_provider="openai",
                model=default.models[0],
                base_url=default.base_url,
                api_key=default.api_key,
            )
        from thinking_settings import thinking_settings
        return init_chat_model(
            model_provider="openai",
            model="deepseek-v4-flash",
            base_url=thinking_settings.DEEPSEEK_API_URL,
            api_key=thinking_settings.DEEPSEEK_API_KEY,
        )
    return init_chat_model(
        model_provider="openai",
        model=model_name,
        base_url=provider.base_url,
        api_key=provider.api_key,
    )