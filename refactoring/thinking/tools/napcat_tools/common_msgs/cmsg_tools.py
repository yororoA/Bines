from smolagents import tool
from .types import SEND_MSG
from .base import _call_api


@tool("send qq message")
def send_msg(msg: SEND_MSG) -> dict | str:
    """
    通过 QQ 发送消息

    Args:
        msg (SEND_MSG): 要发送的消息

    Returns:
        dict|str: 发送结果
    """
    return _call_api("send_msg", msg.model_dump())