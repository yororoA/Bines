from pydantic_settings import BaseSettings, SettingsConfigDict


class ThinkingSettings(BaseSettings):
    MODEL_LIST: list[str]
    MODEL_SELECTED: str
    MIMO_API_URL: str
    MIMO_API_KEY: str
    DEEPSEEK_API_URL: str
    DEEPSEEK_API_KEY: str
    DEEPSEEK_THINKING_API_KEY: str
    DEEPSEEK_SUMMARY_API_KEY: str
    DEEPSEEK_BORED_API_KEY: str
    RAG_EMBEDDING_MODEL: str
    E2B_API_KEY: str

    model_config = SettingsConfigDict(
        env_file="thinking.env", env_file_encoding="utf-8"
    )


thinking_settings = ThinkingSettings()
