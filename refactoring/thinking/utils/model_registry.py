from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelProvider:
    name: str
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)

    def matches(self, model_name: str) -> bool:
        return model_name in self.models


class ModelProviderRegistry:
    def __init__(self):
        self._providers: list[ModelProvider] = []

    def register(self, provider: ModelProvider):
        existing = [p for p in self._providers if p.name == provider.name]
        for p in existing:
            self._providers.remove(p)
        self._providers.append(provider)

    def register_from_dict(self, name: str, base_url: str, api_key: str, models: list[str]):
        self.register(ModelProvider(name=name, base_url=base_url, api_key=api_key, models=models))

    def find(self, model_name: str) -> ModelProvider | None:
        for provider in self._providers:
            if provider.matches(model_name):
                return provider
        return None

    def get_default(self) -> ModelProvider | None:
        return self._providers[0] if self._providers else None

    @property
    def all_model_names(self) -> list[str]:
        names = []
        for p in self._providers:
            names.extend(p.models)
        return names

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": [
                {
                    "name": p.name,
                    "models": p.models,
                }
                for p in self._providers
            ]
        }


_model_registry = ModelProviderRegistry()


def get_model_registry() -> ModelProviderRegistry:
    return _model_registry


def register_default_providers():
    from thinking_settings import thinking_settings

    registry = get_model_registry()

    if thinking_settings.DEEPSEEK_API_URL and thinking_settings.DEEPSEEK_API_KEY:
        registry.register(ModelProvider(
            name="deepseek",
            base_url=thinking_settings.DEEPSEEK_API_URL,
            api_key=thinking_settings.DEEPSEEK_API_KEY,
            models=["deepseek-v4-flash", "deepseek-v4-pro"],
        ))

    if thinking_settings.MIMO_API_URL and thinking_settings.MIMO_API_KEY:
        registry.register(ModelProvider(
            name="mimo",
            base_url=thinking_settings.MIMO_API_URL,
            api_key=thinking_settings.MIMO_API_KEY,
            models=["mimo-v2.5", "mimo-v2.5-pro"],
        ))


_registry_initialized = False
_registry_init_lock = threading.Lock()


def ensure_registry_initialized():
    global _registry_initialized
    if not _registry_initialized:
        with _registry_init_lock:
            if not _registry_initialized:
                register_default_providers()
                _registry_initialized = True