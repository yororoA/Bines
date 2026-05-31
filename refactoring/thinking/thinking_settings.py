from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator


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
    NAPCAT_WS_RECONNECT_TIMEOUT:int = 5
    NAPCAT_WS_API_RESPONSE_TIMEOUT:int = 15
    BOT_NUMBER: str = ""
    DEBOUNCE_SECONDS: float = 3.0
    WORKFLOW_TIMEOUT_SECONDS: float = 120.0
    LLM_REQUEST_TIMEOUT_SECONDS: float = 30.0
    LLM_MAX_RETRIES: int = 1
    LLM_MAX_TOKENS: int = 4096
    DEDUP_THRESHOLD: float = 0.08
    MAX_INPUT_LENGTH: int = 4096
    DAY_KEY_CUTOFF_HOUR: int = 4
    CONVERGENCE_WINDOW: int = 3
    RETRIEVAL_KNOWLEDGE_K: int = 5
    RETRIEVAL_PERSONA_K: int = 4
    RETRIEVAL_DIARY_K: int = 2
    VISUAL_RECOGNITION_API_KEY: str = ""
    VISUAL_RECOGNITION_API_URL: str = ""
    VISUAL_RECOGNITION_MODEL: str = ""
    VIEWER_PORT: int = 8501

    model_config = SettingsConfigDict(
        env_file="thinking.env", env_file_encoding="utf-8"
    )

    @field_validator('DEBOUNCE_SECONDS')
    @classmethod
    def validate_debounce(cls, v: float) -> float:
        if v <= 0:
            raise ValueError('DEBOUNCE_SECONDS must be positive')
        return v

    @field_validator('WORKFLOW_TIMEOUT_SECONDS')
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError('WORKFLOW_TIMEOUT_SECONDS must be positive')
        return v

    @field_validator('LLM_REQUEST_TIMEOUT_SECONDS')
    @classmethod
    def validate_llm_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError('LLM_REQUEST_TIMEOUT_SECONDS must be positive')
        return v

    @field_validator('LLM_MAX_RETRIES')
    @classmethod
    def validate_llm_retries(cls, v: int) -> int:
        if v < 0:
            raise ValueError('LLM_MAX_RETRIES must be >= 0')
        return v

    @field_validator('DEDUP_THRESHOLD')
    @classmethod
    def validate_dedup(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError('DEDUP_THRESHOLD must be between 0 and 1')
        return v

    @field_validator('MAX_INPUT_LENGTH')
    @classmethod
    def validate_max_input(cls, v: int) -> int:
        if v <= 0:
            raise ValueError('MAX_INPUT_LENGTH must be positive')
        return v


thinking_settings = ThinkingSettings()
