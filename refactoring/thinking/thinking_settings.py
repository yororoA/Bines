from pydantic_settings import BaseSettings, SettingsConfigDict


class ThinkingSettings(BaseSettings):
    MODEL_LIST: list[str] = ["deepseek-v4-flash", "deepseek-v4-pro", "mimo-v2.5", "mimo-v2.5-pro"]
    MODEL_SELECTED: str = "deepseek-v4-flash"
    MIMO_API_URL: str = ""
    MIMO_API_KEY: str = ""
    DEEPSEEK_API_URL: str = ""
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_THINKING_API_KEY: str = ""
    DEEPSEEK_THINKING_MODEL: str = "deepseek-v4-flash"
    RAG_EMBEDDING_MODEL: str = "text-embedding-3-small"
    E2B_API_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file="thinking.env", env_file_encoding="utf-8"
    )


thinking_settings = ThinkingSettings()
