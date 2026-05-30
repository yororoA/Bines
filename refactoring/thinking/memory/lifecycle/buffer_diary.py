from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..vector_store.chroma_store import (
    ChromaMemoryStore,
    get_memory_store,
    COLLECTION_BUFFER,
    COLLECTION_DIARY,
    COLLECTION_SLICED_DIARY,
)
from utils import shared_langchain_model
from utils.time_utils import day_key

logger = logging.getLogger(__name__)

_MAX_DIARY_FRAGMENTS_CHARS = 8000


def add_to_buffer(
    content: str,
    metadata: dict[str, Any] | None = None,
    store: ChromaMemoryStore | None = None,
) -> str:
    memory_store = store or get_memory_store()
    meta = metadata or {}
    meta["source"] = "buffer"
    meta["day_key"] = day_key()
    meta["created_at"] = datetime.now().isoformat()
    return memory_store.add(COLLECTION_BUFFER, content, metadata=meta)


def get_buffer_by_day(
    target_day_key: str | None = None,
    store: ChromaMemoryStore | None = None,
) -> list[dict[str, Any]]:
    memory_store = store or get_memory_store()
    filter_dict: dict[str, Any] = {"source": "buffer"}
    if target_day_key:
        filter_dict["day_key"] = target_day_key
    return memory_store.get_all(COLLECTION_BUFFER, filter=filter_dict)


def get_existing_diary_day_keys(
    store: ChromaMemoryStore | None = None,
) -> list[str]:
    memory_store = store or get_memory_store()
    all_diary = memory_store.get_all(COLLECTION_DIARY)
    seen: set[str] = set()
    for entry in all_diary:
        dk = entry.get("metadata", {}).get("day_key", "")
        if dk:
            seen.add(dk)
    return sorted(seen)


def _summarize_diary_with_llm(buffer_contents: list[str], target_day_key: str) -> str:
    model = shared_langchain_model.get()
    fragments = "\n---\n".join(buffer_contents)
    if len(fragments) > _MAX_DIARY_FRAGMENTS_CHARS:
        fragments = fragments[:_MAX_DIARY_FRAGMENTS_CHARS]
        logger.info("Diary fragments truncated to %d chars for LLM", _MAX_DIARY_FRAGMENTS_CHARS)
    prompt = (
        "You are a diary writer for an AI assistant. "
        "Below are conversation fragments and task summaries from a single day.\n"
        f"Day: {target_day_key}\n\n"
        "Please write a concise daily diary entry summarizing:\n"
        "- What the user talked about and asked for\n"
        "- What tasks were completed\n"
        "- Key decisions or insights\n"
        "- The assistant's notable interactions\n\n"
        "Do NOT include raw conversation. Write in natural narrative style.\n\n"
        f"Fragments:\n{fragments}\n\n"
        "Diary entry:"
    )
    response = model.invoke(prompt)
    return response.content if hasattr(response, "content") else str(response)


def _slice_diary_into_paragraphs(diary_text: str) -> list[str]:
    parts = [p.strip() for p in diary_text.replace("\r", "").split("\n\n") if p.strip()]
    if len(parts) > 1:
        return [p for p in parts if len(p) > 20]
    parts = [p.strip() for p in diary_text.replace("\r", "").split("\n") if p.strip()]
    return [p for p in parts if len(p) > 20]


def consolidate_buffer_to_diary(
    target_day_key: str,
    store: ChromaMemoryStore | None = None,
) -> str | None:
    memory_store = store or get_memory_store()

    buffer_entries = get_buffer_by_day(target_day_key, store=memory_store)
    if not buffer_entries:
        return None

    buffer_contents = [entry["content"] for entry in buffer_entries]
    buffer_ids = [entry["id"] for entry in buffer_entries]

    diary_text = _summarize_diary_with_llm(buffer_contents, target_day_key)

    diary_meta = {
        "source": "diary",
        "day_key": target_day_key,
        "created_at": datetime.now().isoformat(),
    }
    diary_id = memory_store.add(COLLECTION_DIARY, diary_text, metadata=diary_meta)

    if diary_id:
        paragraphs = _slice_diary_into_paragraphs(diary_text)
        for i, para in enumerate(paragraphs):
            slice_meta = {
                "source": "sliced_diary",
                "day_key": target_day_key,
                "diary_id": diary_id,
                "paragraph_index": i,
                "created_at": datetime.now().isoformat(),
            }
            memory_store.add(COLLECTION_SLICED_DIARY, para, metadata=slice_meta)
    else:
        logger.info("Diary for %s is duplicate, skipping slices", target_day_key)

    memory_store.delete_by_ids(COLLECTION_BUFFER, buffer_ids)

    return diary_id