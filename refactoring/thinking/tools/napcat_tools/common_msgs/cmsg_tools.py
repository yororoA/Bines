from typing import Union

from smolagents import tool
from .types import SEND_MSG
from .base import _call_api
from workflow.context_manager import get_context_manager
import logging

logger = logging.getLogger(__name__)


def _extract_text(msg: SEND_MSG) -> str:
    parts = []
    for seg in msg.message:
        if seg.type == "text":
            parts.append(seg.data.text)
    return "\n".join(parts)


@tool
def send_msg(msg: Union[dict, SEND_MSG]) -> dict | str:
    """
    通过 QQ 发送消息

    Args:
        msg (Union[dict, SEND_MSG]): 要发送的消息

    Returns:
        dict|str: 发送结果
    """
    if isinstance(msg, dict):
        msg = SEND_MSG(**msg)
    logger.info("send_msg tool called: message_type=%s, user_id=%s, group_id=%s, message_len=%d",
                msg.message_type, msg.user_id, msg.group_id, len(msg.message))
    result = _call_api("send_msg", msg.model_dump())
    if isinstance(result, dict) and result.get("status") == "ok":
        try:
            reply_text = _extract_text(msg)
            if reply_text:
                ctx = get_context_manager()
                ctx.append_to_list("reply_texts", [reply_text])
        except Exception:
            logger.debug("Failed to extract text from sent message", exc_info=True)
    return result