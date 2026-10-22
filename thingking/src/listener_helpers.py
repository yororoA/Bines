import json
from pathlib import Path
from typing import Any, Dict, Tuple


def update_player_busy_from_control_payload(msg_bytes: bytes, current_busy: bool) -> bool:
    """解析 control 消息并更新播放器忙碌状态。"""
    data = json.loads(msg_bytes.decode("utf-8"))
    cough_status = data.get("cough")
    if cough_status == "start":
        return True
    if cough_status == "end":
        return False
    return current_busy


def is_user_online(presence_state_path: Path) -> bool:
    """读取 presence_state.json，异常时默认在线。"""
    try:
        if presence_state_path.exists():
            with open(presence_state_path, "r", encoding="utf-8") as f:
                presence = json.load(f)
            return bool(presence.get("user_online", True))
    except (json.JSONDecodeError, OSError):
        pass
    return True


def build_bored_prompt(event_data: Dict[str, Any]) -> Tuple[str, str]:
    """
    根据 bored 事件构建提示词与日志。
    返回 (log_line, prompt)。
    """
    visual_reason = event_data.get("visual_stimulus") if isinstance(event_data, dict) else None
    if visual_reason:
        log_line = f"[Thinking] Received bored message (视觉刺激): {str(visual_reason)[:60]}..."
        prompt = (
            "[System Event: Bored - Visual Stimulus] "
            f"(You were zoning out, then you noticed: {visual_reason}. "
            "You want to say something about this new discovery. Initiate a short, natural conversation about what you just saw—"
            "e.g. comment on what they're holding, wearing, or the scene change. "
            "Do not repeat the description verbatim; react in character. Use an enabled local skill only if the user requested an action.)"
        )
    else:
        log_line = "[Thinking] Received bored message, triggering active dialogue..."
        prompt = (
            "[System Event: Bored] "
            "(You feel bored and want to initiate a conversation. "
            "You can act curious, use an enabled local skill when relevant, send QQ messages only when requested, "
            "or start a casual conversation.)"
        )
    return log_line, prompt


def bored_event_rejection(event_data, snapshot, now, *, online=True, busy=False, config=None):
    """Recheck candidate freshness after queueing and before publishing local output."""
    from common.proactive import ProactiveConfig, human_ref, parse_decision
    config = config or ProactiveConfig.from_env()
    if not online or busy:
        return "unavailable"
    if event_data.get("kind") == "connection" and config.quiet(now):
        return "quiet_hours"
    if not 0 <= now - float(event_data.get("timestamp") or 0) <= config.expiry:
        return "expired"
    user = snapshot.get("last_user") or {}
    if not human_ref(user) or human_ref(user) != event_data.get("based_on_user"):
        return "new_user_message"
    owner_ts = float((snapshot.get("last_owner_activity") or {}).get("ts") or 0)
    if owner_ts > float(event_data.get("activity_at") or 0):
        return "owner_active_elsewhere"
    if now - max(float(user.get("ts") or 0), owner_ts) < config.min_idle:
        return "not_idle"
    decision = parse_decision(json.dumps({**event_data, "should_act": True}))
    return "" if decision and event_data.get("id") else "invalid_candidate"
