from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class ContextManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, updates: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(updates)

    def append_to_list(self, key: str, items: list) -> None:
        with self._lock:
            existing = self._data.get(key, [])
            if not isinstance(existing, list):
                existing = []
            self._data[key] = existing + items

    def reset(self) -> None:
        with self._lock:
            self._data.clear()
            logger.debug("ContextManager reset")


_context_manager = ContextManager()


def get_context_manager() -> ContextManager:
    return _context_manager
