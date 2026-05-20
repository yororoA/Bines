from smolagents import OpenAIModel


def generate_sml_model(model_id: str) -> OpenAIModel:
    from thinking_settings import thinking_settings

    if model_id in ["deepseek-v4-flash", "deepseek-v4-pro"]:
        return OpenAIModel(
            model_id=model_id,
            api_base=thinking_settings.DEEPSEEK_API_URL,
            api_key=thinking_settings.DEEPSEEK_API_KEY,
        )
    if model_id in ["mimo-v2.5", "mimo-v2.5-pro"]:
        return OpenAIModel(
            model_id=model_id,
            api_base=thinking_settings.MIMO_API_URL,
            api_key=thinking_settings.MIMO_API_KEY,
        )

    return OpenAIModel(
        model_id="deepseek-v4-flash",
        api_base=thinking_settings.DEEPSEEK_API_URL,
        api_key=thinking_settings.DEEPSEEK_API_KEY,
    )
