from __future__ import annotations

from typing import Any, Protocol


class MemoryStore(Protocol):
    def add(
        self,
        collection: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        doc_id: str | None = None,
    ) -> str | None: ...

    def search(
        self,
        collection: str,
        query: str,
        k: int = 3,
        filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def search_with_filter(
        self,
        collection: str,
        query: str,
        k: int = 3,
        target_day_key: str | None = None,
        extra_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_all(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def count(self, collection: str) -> int: ...

    def delete_by_ids(self, collection: str, ids: list[str]) -> None: ...

    def decay_collection(
        self,
        collection: str,
        half_life_days: float = 30.0,
        min_effective_importance: float = 1.0,
        max_entries: int = 500,
    ) -> int: ...

    def get_collection_names(self) -> list[str]: ...
