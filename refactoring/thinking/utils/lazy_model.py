from __future__ import annotations

import logging
import threading
from typing import Any

from thinking_settings import thinking_settings

logger = logging.getLogger(__name__)


class LazyLangChainModel:
    def __init__(self, model_name: str | None = None):
        self._model_name = model_name
        self._model = None
        self._structured_models: dict[str, Any] = {}
        self._lock = threading.RLock()

    @property
    def model_name(self) -> str:
        return self._model_name or thinking_settings.MODEL_SELECTED

    def get(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from .generate_langchain_model import generate_langchain_model
                    logger.info("LazyLangChainModel: creating model %s", self.model_name)
                    self._model = generate_langchain_model(self.model_name)
                    logger.info("LazyLangChainModel: model created %s", type(self._model).__name__)
        return self._model

    def get_structured(self, schema: type):
        key = schema.__name__
        if key not in self._structured_models:
            with self._lock:
                if key not in self._structured_models:
                    logger.info("LazyLangChainModel: creating structured model for %s", key)
                    self._structured_models[key] = self.get().with_structured_output(schema)
                    logger.info("LazyLangChainModel: structured model created for %s", key)
        return self._structured_models[key]


class LazySmolModel:
    def __init__(self, model_name: str | None = None):
        self._model_name = model_name
        self._model = None
        self._lock = threading.RLock()

    @property
    def model_name(self) -> str:
        return self._model_name or thinking_settings.MODEL_SELECTED

    def get(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from .generate_sml_model import generate_sml_model
                    self._model = generate_sml_model(self.model_name)
        return self._model


shared_langchain_model = LazyLangChainModel()
shared_smol_model = LazySmolModel()