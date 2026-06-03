import copy
import hashlib
import logging
import threading
import uuid

from langchain.messages import AIMessage, HumanMessage
from smolagents import CodeAgent
from smolagents.agents import EMPTY_PROMPT_TEMPLATES
from utils import shared_smol_model
from tools import get_tool_registry, PERFORMER_TOOLS
from ..status import GraphStatus, TaskItem
from ..cancel import get_cancel_event
from ..context_manager import get_context_manager
from memory import PersonaState, PersonaMood, retrieve_for_reply, format_retrieval_results

logger = logging.getLogger(__name__)

_PerformerAgent = None
_PerformerLock = threading.Lock()
_cached_soul_hash: str | None = None
# 注意：_PerformerLock 用于保护 _PerformerAgent 的创建和运行
# 由于 CodeAgent 是全局单例，且 run() 方法可能不是线程安全的，
# 因此需要锁保护。但当前锁范围包括整个 run() 调用，导致所有 workflow 串行化。
# 未来可考虑为每个 workflow 创建独立的 CodeAgent 实例以提高并发性。

_MAX_STEPS = 10


def _soul_hash(soul: str) -> str:
    return hashlib.md5(soul.encode()).hexdigest()[:16]


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


def _get_tool_name(tool) -> str:
    return getattr(tool, "name", type(tool).__name__)


def _build_system_prompt(soul_prompt: str, tool_names: list[str]) -> str:
    tools_section = "\n".join(f"  - {name}" for name in tool_names)

    return (
        "You are an autonomous agent. Handle the user's request completely and independently.\n\n"
        f"Available tools:\n{tools_section}\n\n"
        "CRITICAL RULES:\n"
        "1. Your FIRST response MUST be a <code>...</code> block with executable code. "
        "Do NOT output text or thinking before code.\n"
        "2. Plan your approach: read the user's message, decide what tools you need, "
        "execute step by step using the appropriate tools.\n"
        "3. For replying/sending messages: use send_msg.\n"
        "4. For web search: use webSearch.\n"
        "5. For images: use visualRecognition.\n"
        "6. For time/date questions: use datetime module directly — do NOT search the web.\n"
        "7. Messages to the user must be in Chinese.\n"
        "8. Keep send_msg text short (~10 words). Split long replies into multiple send_msg calls.\n"
        "9. After completing all work, call final_answer with feedback describing what you did.\n"
        "10. feedback description MUST state what you did, e.g., '已通过send_msg回复用户：...' or "
        "'已用webSearch搜索并返回结果'. Do NOT use vague descriptions like '已查询' or 'done'.\n\n"
        "send_msg example:\n"
        "<code>\n"
        "text = '你好呀~'\n"
        "msg = {'message_type': 'private', 'user_id': '123', 'message': [{'type': 'text', 'data': {'text': text}}]}\n"
        "send_msg(msg=msg)\n"
        "feedback = [{'task_id': 'r1', 'description': 'replied'}]\n"
        "final_answer(feedback)\n"
        "</code>\n\n"
        "search example:\n"
        "<code>\n"
        "r = webSearch(query='...')\n"
        "feedback = [{'task_id': 's1', 'description': str(r)}]\n"
        "final_answer(feedback)\n"
        "</code>\n\n"
        "mood tracking: if you used send_msg, add as LAST feedback item:\n"
        'feedback.append({"task_id": "mood_update", "mood_arousal": <0-100>, '
        '"mood_consecutive_triggers": <int>})\n'
    )


def _build_context(state: GraphStatus) -> str:
    ctx = get_context_manager()
    persona_snapshot = ctx.get("persona_snapshot", {})
    persona_mood = ctx.get("persona_mood", {})
    rag_recall = ctx.get("rag_recall", {})
    already_said = ctx.get("already_said", [])
    conversation_history = ctx.get("conversation_history", "")
    soul_prompt = ctx.get("soul_prompt", "")
    thread_id = ctx.get("thread_id", "")

    persona = PersonaState.from_dict(persona_snapshot)

    thread_ctx = _parse_thread_id(thread_id)
    if thread_ctx.get("message_type") == "private":
        ctx.set("target_type", "private")
        ctx.set("target_id", thread_ctx["user_id"])
    elif thread_ctx.get("message_type") == "group":
        ctx.set("target_type", "group")
        ctx.set("target_id", thread_ctx["group_id"])

    parts = []

    mood_dict = persona_mood or {}
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
        parts.append(f"\n{profile_str}")

    if soul_prompt:
        parts.append(f"\n{soul_prompt}")

    if rag_recall and rag_recall.get("formatted"):
        parts.append(f"\n[RAG Context]\n{rag_recall['formatted']}")
    else:
        messages = state.get("messages", [])
        current_msg = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                current_msg = msg.content or ""
                break
        if current_msg:
            try:
                results = retrieve_for_reply(current_msg)
                formatted = format_retrieval_results(results)
                if formatted:
                    parts.append(f"\n[RAG Context]\n{formatted}")
            except Exception:
                logger.warning("RAG retrieval failed, proceeding without context")

    if conversation_history:
        parts.append(f"\n[Conversation History]\n{conversation_history}")

    if already_said:
        parts.append(
            "\n[Already Said] "
            + "; ".join(already_said[-5:])
            + "\nAvoid repeating these points."
        )

    target_type = ctx.get("target_type", "")
    target_id = ctx.get("target_id", "")
    if target_type and target_id:
        parts.append(
            f"\n[Target] message_type={target_type}, id={target_id}. "
            "Use these values in send_msg."
        )

    return "\n".join(parts)


def _extract_mood(feedback_raw) -> dict:
    # 修复：正确设置 prev_arousal 字段，用于判断情绪变化趋势
    # 之前缺失此字段，导致无法判断情绪是从高到低还是从低到高
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
            ctx = get_context_manager()
            prev_mood = ctx.get("persona_mood", {})
            # prev_arousal 取上一轮的 arousal 值，用于判断情绪变化趋势
            return {
                "arousal": new_arousal,
                "consecutive_triggers": new_triggers,
                "prev_arousal": prev_mood.get("arousal", 0),
            }
    except Exception:
        logger.warning("Failed to extract mood from feedback, using default")
    return {"arousal": 0, "consecutive_triggers": 0, "prev_arousal": 0}


def _parse_feedback(feedback_raw) -> list[TaskItem]:
    if isinstance(feedback_raw, str):
        return [TaskItem(task_id=f"task_{uuid.uuid4().hex[:8]}", description=feedback_raw[:500])]

    if isinstance(feedback_raw, list):
        tasks = []
        for item in feedback_raw:
            if isinstance(item, dict):
                tid = item.get("task_id", "")
                if tid == "mood_update":
                    continue
                if "description" not in item:
                    continue
                tasks.append(TaskItem(task_id=tid or f"task_{uuid.uuid4().hex[:8]}", description=item["description"][:500]))
            else:
                tasks.append(TaskItem(task_id=f"task_{uuid.uuid4().hex[:8]}", description=str(item)[:500]))
        return tasks or [TaskItem(task_id=f"task_{uuid.uuid4().hex[:8]}", description="[EMPTY] No feedback produced.")]

    return [TaskItem(task_id=f"task_{uuid.uuid4().hex[:8]}", description=str(feedback_raw)[:500])]


def _post_process(ctx, feedback_raw) -> dict:
    persona_mood = _extract_mood(feedback_raw)
    ctx.set("persona_mood", persona_mood)
    sent_texts = ctx.get("reply_texts", [])
    if sent_texts:
        ctx.append_to_list("already_said", sent_texts)
    return persona_mood


def PerformerNode(state: GraphStatus) -> dict:
    global _PerformerAgent, _cached_soul_hash

    ctx = get_context_manager()

    cancel_event = state.get("cancel_event") or get_cancel_event()
    if cancel_event.is_set():
        logger.info("PerformerNode cancelled")
        return {}

    ctx.set("reply_texts", [])

    registry = get_tool_registry()
    all_tools = registry.get_tools(PERFORMER_TOOLS)
    all_imports = registry.get_authorized_imports(PERFORMER_TOOLS)
    all_tool_names = [_get_tool_name(t) for t in all_tools]

    soul_prompt = ctx.get("soul_prompt", "")
    current_hash = _soul_hash(soul_prompt)

    with _PerformerLock:
        if _PerformerAgent is None or current_hash != _cached_soul_hash:
            base_prompt = _build_system_prompt(soul_prompt, all_tool_names)
            logger.info("PerformerNode: building agent, tools=%s", all_tool_names)
            _PerformerAgent = CodeAgent(
                model=shared_smol_model.get(),
                tools=all_tools,
                additional_authorized_imports=["datetime", *all_imports],
                prompt_templates={**copy.deepcopy(EMPTY_PROMPT_TEMPLATES), "system_prompt": base_prompt},
                max_steps=_MAX_STEPS,
            )
            _cached_soul_hash = current_hash

    context_str = _build_context(state)

    messages = state.get("messages", [])
    user_msg = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            user_msg = msg.content or ""
            break

    task_input = (
        f"{context_str}\n\n"
        f"[User Request]\n{user_msg}\n\n"
        "[Instructions]\n"
        "Handle this request completely. Plan what needs to be done, execute step by step "
        "using the available tools, and when everything is done, send a reply to the user "
        "via send_msg if a reply is needed. If you need to search the web, use webSearch. "
        "If you need to recognize images, use visualRecognition. "
        "Always call final_answer with feedback describing everything you did."
    )

    logger.info("PerformerNode: running task, user_msg=%s", user_msg[:100])
    try:
        with _PerformerLock:
            feedback_raw = _PerformerAgent.run(task_input)
        logger.info("PerformerNode: completed, result=%s", str(feedback_raw)[:200])

        feedback = _parse_feedback(feedback_raw)
        persona_mood = _post_process(ctx, feedback_raw)

        sent_texts = ctx.get("reply_texts", [])
        result: dict = {
            "tasks_done": {"performer": feedback},
            "persona_mood": persona_mood,
        }
        if sent_texts:
            result["messages"] = [AIMessage(content=t) for t in sent_texts]
        return result
    except Exception:
        logger.exception("PerformerNode failed")
        return {
            "tasks_done": {"performer": [TaskItem(
                task_id=f"task_failed_{uuid.uuid4().hex[:8]}",
                description="[TASK_FAILED] PerformerNode execution failed.",
            )]},
            "already_said": [],
            "persona_mood": ctx.get("persona_mood", {}),
        }