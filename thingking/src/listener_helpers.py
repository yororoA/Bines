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
            "Do not repeat the description verbatim; react in character. You may use QQ tools if relevant.)"
        )
    else:
        log_line = "[Thinking] Received bored message, triggering active dialogue..."
        prompt = (
            "[System Event: Bored] "
            "(You feel bored and want to initiate a conversation. "
            "You can act curious, check camera/screen, check or post dynamics (get_moments/add_moment/comment_moment via call_tool_agent), "
            "send QQ message (via send_qq_group_msg/send_qq_private_msg in call_tool_agent), or start a casual conversation.)"
        )
    return log_line, prompt
