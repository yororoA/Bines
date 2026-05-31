from __future__ import annotations

import hashlib
import json as _json
import logging
import threading
import uuid

from smolagents import CodeAgent
from utils import shared_smol_model
from ..status import ReplyInput, TaskItem
from ..cancel import get_cancel_event
from memory import PersonaState, retrieve_for_reply, format_retrieval_results
from tools import get_tool_registry, REPLY_TOOLS

logger = logging.getLogger(__name__)

_ReplyAgent = None
_ReplyTools = None
_ReplyLock = threading.Lock()
_ReplyRunLock = threading.Lock()
_cached_prompt_hash: str | None = None


def _prompt_hash(prompt: str) -> str:
    return hashlib.md5(prompt.encode()).hexdigest()[:16]


def _parse_thread_id(thread_id: str) -> dict[str, str]:
    if not thread_id:
        return {}
    parts = thread_id.split("_", 2)
    if len(parts) >= 3 and parts[0] == "QQ":
        msg_type = parts[1]
        id_val = parts[2]
        if msg_type == "private":
            return {"message_type": "private", "user_id": id_val}
        elif msg_type == "group":
            return {"message_type": "group", "group_id": id_val}
    return {}


def _build_reply_system_prompt(reply_input: ReplyInput) -> str:
    persona = PersonaState.from_dict(reply_input.persona_snapshot)

    thread_ctx = _parse_thread_id(reply_input.thread_id)
    ctx_lines = []
    if thread_ctx.get("message_type") == "private":
        ctx_lines.append(f'send_msg(msg={{"message_type": "private", "user_id": "{thread_ctx["user_id"]}", "message": [{{"type": "text", "data": {{"text": "your reply here"}}}}]}})')
    elif thread_ctx.get("message_type") == "group":
        ctx_lines.append(f'send_msg(msg={{"message_type": "group", "group_id": "{thread_ctx["group_id"]}", "message": [{{"type": "text", "data": {{"text": "your reply here"}}}}]}})')

    ctx_str = "\n".join(ctx_lines) if ctx_lines else ""

    base_prompt = (
        "You are a reply agent. Your ONLY job is to send a message to the user via the send_msg tool.\n"
        "CRITICAL RULES:\n"
        "1. You MUST call `send_msg(...)` to deliver your reply. Do NOT use print() — it does NOT send messages.\n"
        "2. After calling send_msg, produce a feedback list and call final_answer.\n"
        "3. The reply text must be in Chinese.\n\n"
        "Example workflow:\n"
        "<code>\n"
        f"{ctx_str}\n"
        "feedback = [{\"task_id\": \"reply_1\", \"description\": \"replied to user\"}]\n"
        "final_answer(feedback)\n"
        "</code>\n\n"
        "IMPORTANT: Always wrap code in <code>...</code> tags. Never output text outside code blocks.\n"
        "IMPORTANT: Call `final_answer(feedback)` after send_msg. Do not add extra commentary.\n\n"
        "IMPORTANT: After calling send_msg, your feedback list MUST include a mood entry with your new emotional state.\n"
        "Add this as the LAST item in the feedback list:\n"
        'feedback.append({"task_id": "mood_update", "mood_arousal": <0-100>, "mood_consecutive_triggers": <int>})\n'
        "mood_arousal: 0=fully relaxed, 30=slightly flustered, 60=tsundere, 85+=fully flustered.\n"
        "mood_consecutive_triggers: how many consecutive turns the user has been teasing/flattering you.\n"
    )

    parts = [base_prompt]

    if reply_input.soul_prompt:
        parts.append(reply_input.soul_prompt)

    mood_dict = reply_input.persona_mood or {}
    from memory.persona_state import PersonaMood
    mood = PersonaMood.from_dict(mood_dict)
    prev_arousal = mood_dict.get("prev_arousal", mood.arousal)

    arousal = mood.arousal
    if arousal <= 30:
        mood_line = "[Mood] 当前心境：轻松活泼。以好奇、调侃的语气回复。"
    elif arousal <= 60:
        mood_line = "[Mood] 当前心境：略带不自在。虽然还在吐槽，但语调里藏着一丝慌乱。"
    elif arousal <= 85:
        mood_line = "[Mood] 当前心境：傲娇害羞。嘴硬否认、语速偏快、可用颜文字。"
    else:
        mood_line = "[Mood] 当前心境：炸毛害羞。极度慌乱、音量拔高、可能结巴、可用颜文字。"

    parts.append(mood_line)

    if mood.consecutive_triggers >= 2:
        parts.append(f"连续被调侃{mood.consecutive_triggers}轮了，你快要撑不住了。")

    if prev_arousal - arousal > 20:
        parts.append("你正在从害羞中恢复，可能用过度活泼来掩盖尴尬。")

    profile_str = persona.to_prompt_string()
    if profile_str:
        parts.append(f"\n\n{profile_str}")

    rag = reply_input.rag_recall
    if rag and rag.get("formatted"):
        parts.append(f"\n\n[RAG Context]\n{rag['formatted']}")
    elif reply_input.message:
        results = retrieve_for_reply(reply_input.message)
        formatted = format_retrieval_results(results)
        if formatted:
            parts.append(f"\n\n[RAG Context]\n{formatted}")

    if reply_input.already_said:
        already_said_str = (
            "\n\n[Already Said] You have already told the user: "
            + "; ".join(reply_input.already_said[-5:])
            + "\nAvoid repeating these points."
        )
        parts.append(already_said_str)

    return "\n".join(parts)


def _extract_mood(feedback_raw, reply_input: ReplyInput) -> dict:
    try:
        mood_item = None
        items = feedback_raw if isinstance(feedback_raw, list) else []
        for item in reversed(items):
            if isinstance(item, dict) and "mood_arousal" in item:
                mood_item = item
                break
        if mood_item:
            new_arousal = float(mood_item.get("mood_arousal", 0))
            new_triggers = int(mood_item.get("mood_consecutive_triggers", 0))
            new_arousal = max(0.0, min(100.0, new_arousal))
            new_triggers = max(0, new_triggers)
            prev_mood = reply_input.persona_mood or {}
            return {
                "arousal": new_arousal,
                "consecutive_triggers": new_triggers,
                "prev_arousal": prev_mood.get("arousal", 0),
            }
    except Exception:
        logger.warning("Failed to extract mood from feedback, using default")
    return {"arousal": 0, "consecutive_triggers": 0, "prev_arousal": 0}


def ReplyNode(reply_input: ReplyInput) -> dict[str, list[TaskItem]]:
    global _ReplyAgent, _ReplyTools, _cached_prompt_hash

    try:
        cancel_event = get_cancel_event()
        if cancel_event.is_set():
            logger.info("ReplyNode cancelled")
            fallback = [TaskItem(task_id=f"reply_cancel_{uuid.uuid4().hex[:8]}", description="[CANCELLED] Workflow was cancelled due to timeout.")]
            return {
                "tasks_done": {"final_reply" if reply_input.Final else "advance_reply": fallback},
                "already_said": [],
                "persona_mood": {},
            }

        prompt = _build_reply_system_prompt(reply_input)
        current_hash = _prompt_hash(prompt)

        needs_rebuild = _ReplyAgent is None or current_hash != _cached_prompt_hash
        if needs_rebuild:
            with _ReplyLock:
                if _ReplyAgent is None or current_hash != _cached_prompt_hash:
                    if _ReplyTools is None:
                        registry = get_tool_registry()
                        _ReplyTools = registry.get_tools(REPLY_TOOLS)
                        logger.info(
                            "ReplyAgent tools loaded: count=%d, names=%s",
                            len(_ReplyTools),
                            [getattr(t, "__name__", type(t).__name__) for t in _ReplyTools],
                        )

                    import copy

                    from smolagents.agents import EMPTY_PROMPT_TEMPLATES

                    _ReplyAgent = CodeAgent(
                        model=shared_smol_model.get(),
                        name="ReplyAgent",
                        description="Agent used to reply to the user.",
                        tools=_ReplyTools,
                        additional_authorized_imports=["datetime"],
                        prompt_templates={**copy.deepcopy(EMPTY_PROMPT_TEMPLATES), "system_prompt": prompt},
                        max_steps=6,
                    )
                    _cached_prompt_hash = current_hash

        logger.info("ReplyNode: calling agent with message=%s", reply_input.message[:100] if reply_input.message else "None")
        with _ReplyRunLock:
            thread_ctx = _parse_thread_id(reply_input.thread_id)
            task_str = _json.dumps(
                {"tasks": reply_input.tasks, "message": reply_input.message, "thread_context": thread_ctx},
                ensure_ascii=False,
            )
            feedback_raw = _ReplyAgent.run(task_str)
        logger.info("ReplyNode: agent returned type=%s", type(feedback_raw).__name__)

        if isinstance(feedback_raw, str):
            feedback = [TaskItem(task_id=f"reply_{uuid.uuid4().hex[:8]}", description=feedback_raw[:500])]
        elif isinstance(feedback_raw, list):
            feedback = [
                TaskItem(**item) if isinstance(item, dict)
                else TaskItem(task_id=f"reply_{uuid.uuid4().hex[:8]}", description=str(item)[:500])
                for item in feedback_raw
            ]
        else:
            feedback = [TaskItem(task_id=f"reply_{uuid.uuid4().hex[:8]}", description=str(feedback_raw)[:500])]

        already_said_entries = [item.description for item in feedback if item.description]

        persona_mood = _extract_mood(feedback_raw, reply_input)

        return {
            "tasks_done": {"final_reply" if reply_input.Final else "advance_reply": feedback},
            "already_said": already_said_entries,
            "persona_mood": persona_mood,
        }
    except Exception:
        logger.exception("ReplyNode failed")
        fallback = [TaskItem(task_id=f"reply_error_{uuid.uuid4().hex[:8]}", description="[REPLY_FAILED] Could not generate a reply. Please try rephrasing your message.")]
        return {
            "tasks_done": {"final_reply" if reply_input.Final else "advance_reply": fallback},
            "already_said": [],
            "persona_mood": {},
        }