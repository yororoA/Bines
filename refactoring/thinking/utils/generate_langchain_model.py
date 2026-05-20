from langchain.chat_models import init_chat_model, BaseChatModel


def generate_langchain_model(model_name: str) -> BaseChatModel:
    if model_name in ["deepseek-v4-flash", "deepseek-v4-pro"]:
        return init_chat_model(
            model_provider="openai",
            model=model_name,
            base_url=thinking_settings.DEEPSEEK_API_URL,
            api_key=thinking_settings.DEEPSEEK_API_KEY,
        )
    if model_name in ["mimo-v2.5", "mimo-v2.5-pro"]:
        return init_chat_model(
            model_provider="openai",
            model=model_name,
            base_url=thinking_settings.MIMO_API_URL,
            api_key=thinking_settings.MIMO_API_KEY,
        )
    return init_chat_model(
        model_provider="openai",
        model="deepseek-v4-flash",
        base_url=thinking_settings.DEEPSEEK_API_URL,
        api_key=thinking_settings.DEEPSEEK_API_KEY,
    )
