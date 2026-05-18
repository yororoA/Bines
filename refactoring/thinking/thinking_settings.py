from pydantic_settings import BaseSettings, SettingsConfigDict


class ThinkingSettings(BaseSettings):
    MODEL_LIST: list[str] = ["deepseek-v4-flash", "deepseek-v4-pro", "mimo-v2.5", "mimo-v2.5-pro"]
    MODEL_SELECTED: str = "deepseek-v4-flash"
    MIMO_API_URL: str = ""
    MIMO_API_KEY: str = ""
    DEEPSEEK_API_URL: str = ""
    DEEPSEEK_API_KEY: str = ""
    RAG_EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"
    RAG_PERSIST_DIR: str = "memory_data/chroma_db"
    HF_ENDPOINT: str = ""
    E2B_API_KEY: str = ""
    NAPCAT_WS_SERVER:str = ""
    NAPCAT_WS_TOKEN:str = ""

    model_config = SettingsConfigDict(
        env_file="thinking.env", env_file_encoding="utf-8"
    )


thinking_settings = ThinkingSettings()
