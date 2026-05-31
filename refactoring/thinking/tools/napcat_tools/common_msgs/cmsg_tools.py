from typing import Union

from smolagents import tool
from .types import SEND_MSG
from .base import _call_api
import logging

logger = logging.getLogger(__name__)


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
    return _call_api("send_msg", msg.model_dump())