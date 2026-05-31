from __future__ import annotations

import json as _json
import logging
import threading
from typing import Any, Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from thinking_settings import thinking_settings

logger = logging.getLogger(__name__)


def _make_json_schema_injector(schema: type[BaseModel]):
    schema_json = _json.dumps(schema.model_json_schema(), ensure_ascii=False)

    def _inject(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        keyword = "JSON"
        suffix = (
            f"\nYou MUST respond with a single {keyword.upper()} object that matches this schema:\n"
            f"```json\n{schema_json}\n```"
        )
        result = list(messages)
        if not result:
            result.append(SystemMessage(content=suffix.strip()))
            return result

        first = result[0]
        if isinstance(first, SystemMessage):
            result[0] = SystemMessage(content=first.content + suffix)
        elif hasattr(first, "type") and first.type == "system":
            result[0] = SystemMessage(content=first.content + suffix)
        else:
            result.insert(0, SystemMessage(content=suffix.strip()))
        return result

    return RunnableLambda(_inject)


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
                    structured = self.get().with_structured_output(schema, method="json_mode")
                    self._structured_models[key] = _make_json_schema_injector(schema) | structured
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