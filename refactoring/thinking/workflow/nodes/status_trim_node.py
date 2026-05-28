from __future__ import annotations

import logging
from langchain.messages import SystemMessage
from ..status import GraphStatus, _RESET, MESSAGE_WINDOW_SIZE, MESSAGE_TRIM_SIZE
from utils import shared_langchain_model

logger = logging.getLogger(__name__)


def _summarize_messages(messages: list) -> str:
    if not messages:
        return ""

    text_parts = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content:
            text_parts.append(f"[{role}] {content}")

    if not text_parts:
        return ""

    combined = "\n".join(text_parts)
    model = shared_langchain_model.get()

    prompt = (
        "Summarize the following conversation into a concise context note. "
        "Preserve key facts, decisions, topics discussed, and any user preferences mentioned. "
        "Keep it under 200 words.\n\n"
        f"Conversation:\n{combined}\n\nSummary:"
    )

    try:
        response = model.invoke(prompt)
        return response.content if hasattr(response, "content") else str(response)
    except Exception:
        logger.exception("Failed to summarize messages for window trim")
        return ""


def StatusTrimNode(state: GraphStatus) -> dict:
    messages = state.get("messages", [])

    result = {
        "tasks_done": _RESET,
        "thoughts": _RESET,
        "iteration_count": 0,
        "persona_snapshot": {},
        "rag_recall": {},
        "soul_prompt": "",
        "already_said": _RESET,
    }

    if len(messages) >= MESSAGE_WINDOW_SIZE:
        to_summarize = messages[:MESSAGE_TRIM_SIZE]
        to_keep = messages[MESSAGE_TRIM_SIZE:]

        summary = _summarize_messages(to_summarize)
        if summary:
            summary_msg = SystemMessage(
                content=f"[Earlier conversation summary]\n{summary}"
            )
            result["messages"] = _RESET
            result["messages"] = [summary_msg] + to_keep
        else:
            result["messages"] = _RESET
            result["messages"] = to_keep

        logger.info(
            "Message window trimmed: %d -> %d messages",
            len(messages), len(to_keep) + (1 if summary else 0),
        )

    return result
