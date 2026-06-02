from smolagents import OpenAIModel

from .model_registry import get_model_registry, ensure_registry_initialized


def generate_sml_model(model_id: str) -> OpenAIModel:
    ensure_registry_initialized()
    registry = get_model_registry()
    provider = registry.find(model_id)
    if not provider:
        default = registry.get_default()
        if default:
            return OpenAIModel(
                model_id=default.models[0],
                api_base=default.base_url,
                api_key=default.api_key,
            )
        from thinking_settings import thinking_settings
        return OpenAIModel(
            model_id=model_id,
            api_base=thinking_settings.DEEPSEEK_API_URL,
            api_key=thinking_settings.DEEPSEEK_API_KEY,
        )
    return OpenAIModel(
        model_id=model_id,
        api_base=provider.base_url,
        api_key=provider.api_key,
    )