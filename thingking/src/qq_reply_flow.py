import traceback
from typing import Any, Callable, Dict, Optional


def handle_qq_reply_if_needed(
    *,
    source: Optional[str],
    extra_data: Optional[Dict[str, Any]],
    to_save: str,
    exited_due_to_interrupt: bool,
    send_qq_reply_func: Callable[[str, dict], str],
) -> str:
    """在 QQ 场景下发送回复，返回实际发出的文本（未发送时为空串）。"""
    if source != "QQ" or not extra_data:
        return ""
    if not to_save or not str(to_save).strip() or exited_due_to_interrupt:
        return ""

    try:
        qq_ctx = extra_data.get("qq_context") or extra_data
        return send_qq_reply_func(to_save, qq_ctx) or ""
    except Exception as e:
        print(f"[Thinking] Failed to send QQ reply: {e}", flush=True)
        traceback.print_exc()
        return ""
