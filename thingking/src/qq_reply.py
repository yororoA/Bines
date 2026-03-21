import re
import time
from typing import Dict, List


def normalize_reply_text(reply_raw: str) -> str:
    """剥离动作描述（如（微笑）），若结果为空则回退原文。"""
    base = (reply_raw or "").strip()
    if not base:
        return ""
    cleaned = re.sub(r"[（(][^）)]*[）)]", "", base).strip()
    return cleaned or base


def split_reply_segments(reply_text: str) -> List[str]:
    """先按双空格分段；若分段不足且文本较长，再按句号/问号/叹号回退分段。"""
    text = (reply_text or "").strip()
    if not text:
        return []

    segments = [s.strip() for s in text.split("  ") if s.strip()]
    if len(segments) <= 1 and len(text) > 30:
        fallback = re.split(r"(?<=[。？！])\s*", text)
        fallback = [s.strip() for s in fallback if s.strip()]
        if len(fallback) > 1:
            segments = fallback

    return segments or [text]


def send_qq_reply(reply_raw: str, qq_ctx: Dict, delay_sec: float = 0.8) -> str:
    """
    发送 QQ 回复（群聊/私聊自动分流）。
    返回规范化后的 reply_msg（用于上层写入 Buffer）。
    """
    reply_msg = normalize_reply_text(reply_raw)
    if not reply_msg:
        return ""

    from tools.qq_tool import send_qq_private_msg, send_qq_group_msg

    user_id = qq_ctx.get("user_id")
    group_id = qq_ctx.get("group_id")
    segments = split_reply_segments(reply_msg)

    for i, seg in enumerate(segments):
        if group_id:
            try:
                gid = int(group_id)
                sender_uid = user_id
                print(
                    f"[Thinking] Sending QQ Group Reply to {gid} (At: {sender_uid}) (Seg {i+1}/{len(segments)}): {seg}",
                    flush=True,
                )
                current_at = sender_uid if i == 0 else None
                send_qq_group_msg(gid, seg, at_user_id=current_at)
            except ValueError:
                print(f"[Thinking] Invalid group_id for QQ reply: {group_id}", flush=True)
        elif user_id:
            try:
                uid = int(user_id)
                print(
                    f"[Thinking] Sending QQ Private Reply to {uid} (Seg {i+1}/{len(segments)}): {seg}",
                    flush=True,
                )
                send_qq_private_msg(uid, seg)
            except ValueError:
                pass

        if i < len(segments) - 1:
            time.sleep(delay_sec)

    return reply_msg
