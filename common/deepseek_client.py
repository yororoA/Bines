"""One process-wide DeepSeek client configured from the root settings."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_API_TIMEOUT,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_THINKING,
    require_env,
)


class _Completions:
    def __init__(self, inner: Any):
        self._inner = inner

    def create(self, **kwargs: Any):
        extra_body = {"thinking": {"type": DEEPSEEK_THINKING}} if DEEPSEEK_THINKING else {}
        extra_body.update(kwargs.pop("extra_body", None) or {})
        return self._inner.create(extra_body=extra_body or None, **kwargs)


class _Chat:
    def __init__(self, inner: Any):
        self.completions = _Completions(inner.completions)


class DeepSeekClient:
    """OpenAI SDK client that applies the configured thinking mode to every chat request."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.chat = _Chat(inner.chat)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@lru_cache(maxsize=1)
def get_deepseek_client() -> DeepSeekClient:
    """Return the shared OpenAI-compatible client for all DeepSeek text tasks."""
    # Imported lazily: services without the openai package (e.g. Speaking) still load the tool registry.
    from openai import OpenAI

    return DeepSeekClient(OpenAI(
        api_key=require_env("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY),
        base_url=DEEPSEEK_BASE_URL,
        timeout=float(DEEPSEEK_API_TIMEOUT),
        max_retries=2,
    ))
