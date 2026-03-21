import time
import traceback
from typing import Any, Callable, Dict, Optional


def handle_qq_reply_if_needed(
    *,
    source: Optional[str],
    extra_data: Optional[Dict[str, Any]],
    to_save: str,
    exited_due_to_interrupt: bool,
    send_qq_reply_func: Callable[[str, dict], str],
    get_qq_buffer_manager_func: Callable[[], Any],
) -> None:
    """在 QQ 场景下发送回复并把机器人回复写入 QQ Buffer。"""
    if source != "QQ":
        return
    if not extra_data:
        return
    if not to_save or not str(to_save).strip():
        return
    if exited_due_to_interrupt:
        return

    try:
        qq_ctx = extra_data.get("qq_context") or extra_data
        reply_msg = send_qq_reply_func(to_save, qq_ctx)
        if not reply_msg:
            return

        group_id = qq_ctx.get("group_id")

        try:
            buf_mgr = get_qq_buffer_manager_func()
            bot_meta = {
                "timestamp": time.time(),
                "sender": "Self(Bot)",
                "group_id": qq_ctx.get("group_id"),
                "user_id": qq_ctx.get("user_id"),
                "is_group": bool(qq_ctx.get("group_id")),
            }
            if group_id:
                bot_content = f"[QQ群回复][Bot]: {reply_msg}"
            else:
                bot_content = f"[QQ私聊回复][Bot]: {reply_msg}"

            buf_mgr.add_message(bot_content, bot_meta)
            print("[Thinking] Bot QQ 回复已存入 Buffer", flush=True)
        except Exception as buf_err:
            print(f"[Thinking] Bot 回复存入 Buffer 失败: {buf_err}", flush=True)

    except Exception as e:
        print(f"[Thinking] Failed to send QQ reply: {e}", flush=True)
        traceback.print_exc()
