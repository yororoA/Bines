from __future__ import annotations

import logging
from langchain.messages import SystemMessage
from ..status import GraphStatus, RESET, MESSAGE_WINDOW_SIZE, MESSAGE_TRIM_SIZE
from ..cancel import get_cancel_event
from utils import shared_langchain_model

logger = logging.getLogger(__name__)


def _find_trim_index(messages: list, max_index: int) -> int:
    from langchain.messages import HumanMessage
    best = max_index
    for i in range(min(max_index, len(messages))):
        if isinstance(messages[i], HumanMessage):
            best = i
    return best


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
        "MUST be under 200 words. Output ONLY the summary text, no preamble or labels.\n\n"
        f"Conversation:\n{combined}\n\nSummary:"
    )

    try:
        response = model.invoke(prompt)
        return response.content if hasattr(response, "content") else str(response)
    except Exception:
        logger.exception("Failed to summarize messages for window trim")
        return ""


def StatusTrimNode(state: GraphStatus) -> dict:
    cancel_event = get_cancel_event()
    if cancel_event.is_set():
        logger.info("StatusTrimNode cancelled")
        return {}

    messages = state.get("messages", [])

    result = {
        "tasks_done": RESET,
        "thoughts": RESET,
        "iteration_count": 0,
        "persona_snapshot": {},
        "rag_recall": {},
        "soul_prompt": "",
        "already_said": RESET,
        "diary_triggered_day": state.get("diary_triggered_day", ""),
        "invocation_count": state.get("invocation_count", 0),
    }

    if len(messages) >= MESSAGE_WINDOW_SIZE:
        trim_at = _find_trim_index(messages, MESSAGE_TRIM_SIZE)
        if trim_at >= len(messages):
            trim_at = len(messages)
        to_summarize = messages[:trim_at]
        to_keep = messages[trim_at:]

        summary = _summarize_messages(to_summarize)
        if summary:
            summary_msg = SystemMessage(
                content=f"[Earlier conversation summary]\n{summary}"
            )
            result["messages"] = [summary_msg] + to_keep
        else:
            result["messages"] = to_keep

        logger.info(
            "Message window trimmed: %d -> %d messages",
            len(messages), len(to_keep) + (1 if summary else 0),
        )

    return result
