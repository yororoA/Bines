from __future__ import annotations

from langchain.chat_models import BaseChatModel, init_chat_model

from .model_registry import ensure_registry_initialized, get_model_registry


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require(name: str, value: str | None) -> str:
    value = _normalize(value)
    if not value:
        raise ValueError(
            f"{name} is empty. Please set it in thinking.env (see thinking.env.example)."
        )
    return value


def generate_langchain_model(model_name: str) -> BaseChatModel:
    """Create a LangChain chat model with safe defaults.

    - Fails fast when base_url/api_key are missing (prevents long hangs).
    - Applies per-request timeout/retry settings from thinking_settings.
    """

    ensure_registry_initialized()
    registry = get_model_registry()
    provider = registry.find(model_name)

    from thinking_settings import thinking_settings

    llm_timeout = thinking_settings.LLM_REQUEST_TIMEOUT_SECONDS
    llm_retries = thinking_settings.LLM_MAX_RETRIES

    if provider:
        return init_chat_model(
            model_provider="openai",
            model=model_name,
            base_url=_require("LLM base_url", provider.base_url),
            api_key=_require("LLM api_key", provider.api_key),
            timeout=llm_timeout,
            max_retries=llm_retries,
        )

    default = registry.get_default()
    if default:
        default_model = default.models[0] if default.models else model_name
        return init_chat_model(
            model_provider="openai",
            model=default_model,
            base_url=_require("LLM base_url", default.base_url),
            api_key=_require("LLM api_key", default.api_key),
            timeout=llm_timeout,
            max_retries=llm_retries,
        )

    # No registry providers registered. Fall back to env settings based on model family.
    if model_name.startswith("mimo"):
        base_url = _require("MIMO_API_URL", thinking_settings.MIMO_API_URL)
        api_key = _require("MIMO_API_KEY", thinking_settings.MIMO_API_KEY)
    else:
        base_url = _require("DEEPSEEK_API_URL", thinking_settings.DEEPSEEK_API_URL)
        api_key = _require("DEEPSEEK_API_KEY", thinking_settings.DEEPSEEK_API_KEY)

    return init_chat_model(
        model_provider="openai",
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        timeout=llm_timeout,
        max_retries=llm_retries,
    )