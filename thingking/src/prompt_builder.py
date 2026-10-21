"""Assemble conversation prompts from the persona, a scene policy and the scoped memory context."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import persona_profile
from memory.models import (
    CHANNEL_LOCAL,
    CHANNEL_QQ_GROUP,
    OWNER_SCOPE,
    ROLE_ASSISTANT,
    ROLE_OBSERVED,
)

SCENE_LOCAL = "local"
SCENE_OWNER_QQ = "owner_qq_private"
SCENE_QQ_PRIVATE = "qq_private"
SCENE_QQ_GROUP = "qq_group"

_QQ_TOOL_POLICY = (
    "你无法通过屏幕或摄像头看到对方。本轮只能使用技能清单中允许的 QQ 消息、网页搜索、时间与动态动作，"
    "禁止调用视觉、屏幕和本地桌面操作。不需要技能就能回答时直接回复。回复要极度简洁，直击重点。"
)
_PRIVACY_POLICY = "对方不是管理员：不要透露管理员的私人信息，也不要提及你和管理员的私下对话。"

_OPERATING_RULES = [
    "需要真实世界操作时，使用本轮启用的本地技能；不要编造执行结果或视觉描述。",
    "用户问到过去的对话或记忆时，直接根据 <memory_recall> 和对话历史回答。",
    "收到 [System Event: Long Silence] 或 [System Event: Bored] 时，可以随口开启话题，也可以保持安静；只有情境明确需要时才使用技能。",
]

_STATE_LABELS = (
    ("current_location", "所在位置"),
    ("relationship_level", "与管理员的关系"),
    ("active_quest", "正在进行的约定或任务"),
    ("important_thing", "最近的重要情况"),
)

_QQ_PREFIX = re.compile(r"^\[QQ(?:群消息|私聊)\]\[[^\]]*\]:\s*")


def strip_qq_prefix(text: str) -> str:
    """Remove the ``[QQ群消息][name]:`` markers Classification adds, line by line (merged messages)."""
    return "\n".join(_QQ_PREFIX.sub("", line) for line in (text or "").splitlines()).strip()


def scene_for(envelope: Dict[str, Any]) -> str:
    channel = envelope.get("channel_type")
    if channel == CHANNEL_LOCAL:
        return SCENE_LOCAL
    if channel == CHANNEL_QQ_GROUP:
        return SCENE_QQ_GROUP
    return SCENE_OWNER_QQ if envelope.get("scope") == OWNER_SCOPE else SCENE_QQ_PRIVATE


def _scene_policy(envelope: Dict[str, Any], scene: str) -> str:
    owner_title = persona_profile.load_persona()["identity"]["owner_title"]
    name = envelope.get("actor_name") or "对方"
    is_owner = bool(envelope.get("owner_identity"))
    if scene == SCENE_LOCAL:
        return f"--- 当前场景 ---\n你正在电脑前和{owner_title}直接聊天（语音或文字）。"
    if scene == SCENE_OWNER_QQ:
        return f"--- 当前场景 ---\n{owner_title}正在通过 QQ 私聊找你。{_QQ_TOOL_POLICY}"
    if scene == SCENE_QQ_PRIVATE:
        return f"--- 当前场景 ---\nQQ 用户「{name}」正在私聊你。{_PRIVACY_POLICY}{_QQ_TOOL_POLICY}"
    identity_policy = (
        f"发言者已通过绑定的 QQ 账号确认为{owner_title}本人。"
        f"必须承认这一身份；当对方问自己是不是{owner_title}、你是否认识对方时，要直接肯定回答。"
        if is_owner else _PRIVACY_POLICY
    )
    lines = [
        f"--- 当前场景 ---\n你在 QQ 群 {envelope.get('external_group_id')} 中，「{name}」在群里@了你。"
        f"群成员不一定认识{owner_title}，{identity_policy}{_QQ_TOOL_POLICY}"
    ]
    if is_owner:
        lines.append(
            f"这条群消息来自{owner_title}。请优先响应并明确承认对方身份，"
            "但群聊仍是公开场景：不要公开账号号码、私聊内容、私人记忆或仅限私聊的状态。"
        )
    return "\n".join(lines)


def _state_block(state: Dict[str, Any]) -> str:
    if not state:
        return ""
    lines = []
    npc = state.get("npc_state") or {}
    if npc.get("attire"):
        lines.append(f"- 你现在的穿着：{npc['attire']}")
    if npc.get("current_activity"):
        lines.append(f"- 你正在做的事：{npc['current_activity']}")
    if npc.get("visual_module_status"):
        lines.append(f"- 视觉模块状态：{npc['visual_module_status']}")
    for key, label in _STATE_LABELS:
        if state.get(key):
            lines.append(f"- {label}：{state[key]}")
    if state.get("inventory"):
        lines.append(f"- 持有物品：{'；'.join(map(str, state['inventory']))}")
    highlights = state.get("memory_highlights") or []
    if highlights:
        lines.append("- 重要回忆：")
        lines.extend(f"  · {item}" for item in highlights)
    return "--- 当前状态 ---\n" + "\n".join(lines) if lines else ""


def _profile_block(context: Dict[str, Any], scene: str) -> str:
    facts = [item["content"] for item in context.get("profile") or []]
    if not facts:
        return ""
    owner_title = persona_profile.load_persona()["identity"]["owner_title"]
    who = owner_title if scene in (SCENE_LOCAL, SCENE_OWNER_QQ) else (context["envelope"].get("actor_name") or "对方")
    return f"--- 你记得的关于{who}的事 ---\n" + "\n".join(f"- {fact}" for fact in facts)


def _fmt_time(ts: Optional[float], with_date: bool = True) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts)).strftime("%Y/%m/%d %H:%M" if with_date else "%H:%M")


def _channel_label(conversation_id: str) -> str:
    if conversation_id == "local":
        return "本机"
    if conversation_id.startswith("qq_private:") or ":private:" in conversation_id:
        return "QQ私聊"
    return conversation_id


def _memory_recall_block(context: Dict[str, Any]) -> str:
    parts: List[str] = []
    bridge = context.get("bridge")
    if bridge:
        parts.append(f"[较早的对话 {bridge.get('day_key') or ''}]\n{bridge['content']}")
    for item in context.get("recalled") or []:
        label = {"diary": "日记", "episode": "回忆", "excerpt": "对话片段"}.get(item["kind"], "回忆")
        parts.append(f"[{label} {item.get('day_key') or ''}]\n{item['content']}")
    others = context.get("other_channels") or []
    if others:
        lines = [
            f"[{_fmt_time(m['ts'])}][{_channel_label(m['conversation_id'])}] "
            f"{'你' if m['role'] == ROLE_ASSISTANT else (m.get('actor_name') or '管理员')}: {m['content']}"
            for m in others
        ]
        parts.append("[其他渠道的近期对话]\n" + "\n".join(lines))
    if not parts:
        return ""
    return "<memory_recall>\n" + "\n---\n".join(parts) + "\n</memory_recall>"


def _working_messages(context: Dict[str, Any], scene: str) -> List[Dict[str, str]]:
    working = context.get("working") or []
    if scene == SCENE_QQ_GROUP:
        if not working:
            return []
        lines = [
            f"[{_fmt_time(m['ts'], with_date=False)}] "
            f"{'你' if m['role'] == ROLE_ASSISTANT else (m.get('actor_name') or m.get('actor_id'))}"
            f"{'' if m['role'] != ROLE_OBSERVED else '(未@你)'}: {m['content']}"
            for m in working
        ]
        return [{"role": "system", "content": "--- 群聊近况 ---\n" + "\n".join(lines)}]
    messages = []
    for m in working:
        if m["role"] == ROLE_ASSISTANT:
            messages.append({"role": "assistant", "content": m["content"]})
        else:
            messages.append({"role": "user", "content": f"[{_fmt_time(m['ts'])}]: [{m['content']}]"})
    return messages


def build_conversation_messages(context: Dict[str, Any]) -> List[Dict[str, str]]:
    """Messages to send before the current user input (which the caller appends)."""
    envelope = context.get("envelope") or {}
    scene = scene_for(envelope)
    system_sections = [
        persona_profile.character_prompt(),
        _scene_policy(envelope, scene),
        "--- 行为准则 ---\n" + "\n".join(f"{i}. {rule}" for i, rule in enumerate(_OPERATING_RULES, 1)),
        _state_block(context.get("state") or {}),
        _profile_block(context, scene),
    ]
    messages = [{"role": "system", "content": "\n\n".join(s for s in system_sections if s)}]
    recall = _memory_recall_block(context)
    if recall:
        messages.append({"role": "system", "content": recall})
    messages.extend(_working_messages(context, scene))
    messages.append({"role": "system", "content": persona_profile.style_guard()})
    return messages


def format_current_input(user_input: str, now: datetime) -> str:
    return f"[{now.strftime('%Y/%m/%d %H:%M')}]: [{user_input}]"


def empty_context(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Context used when the Memory Server is unreachable: persona and scene only."""
    return {"envelope": envelope, "working": [], "recalled": [], "profile": [], "state": {}, "bridge": None,
            "other_channels": [], "last_user_ts": None}
