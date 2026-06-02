from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class ContextManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._contexts: dict[str, dict[str, Any]] = {}
        self._local = threading.local()

    def set_active_thread(self, thread_id: str) -> None:
        self._local.thread_id = thread_id

    def _get_active_thread(self) -> str:
        tid = getattr(self._local, "thread_id", None)
        if tid is None:
            raise RuntimeError(
                "ContextManager: no active thread_id. "
                "Call set_active_thread() before accessing context."
            )
        return tid

    def _get_ctx(self, thread_id: str | None = None) -> dict[str, Any]:
        tid = thread_id or self._get_active_thread()
        with self._lock:
            if tid not in self._contexts:
                self._contexts[tid] = {}
            return self._contexts[tid]

    def get(self, key: str, default: Any = None, thread_id: str | None = None) -> Any:
        ctx = self._get_ctx(thread_id)
        return ctx.get(key, default)

    def get_all(self, thread_id: str | None = None) -> dict[str, Any]:
        ctx = self._get_ctx(thread_id)
        with self._lock:
            return dict(ctx)

    def set(self, key: str, value: Any, thread_id: str | None = None) -> None:
        ctx = self._get_ctx(thread_id)
        ctx[key] = value

    def update(self, updates: dict[str, Any], thread_id: str | None = None) -> None:
        ctx = self._get_ctx(thread_id)
        ctx.update(updates)

    def append_to_list(self, key: str, items: list, thread_id: str | None = None) -> None:
        ctx = self._get_ctx(thread_id)
        existing = ctx.get(key, [])
        if not isinstance(existing, list):
            existing = []
        ctx[key] = existing + items

    def reset(self, thread_id: str | None = None) -> None:
        tid = thread_id or getattr(self._local, "thread_id", None)
        if tid is None:
            return
        with self._lock:
            self._contexts.pop(tid, None)
            logger.debug("ContextManager reset for thread=%s", tid)


_context_manager = ContextManager()


def get_context_manager() -> ContextManager:
    return _context_manager
