from smolagents import tool
from .types import SEND_POKE
from .base import _call_api


@tool("send poke")
def send_poke(msg: SEND_POKE) -> dict | str:
    """
    发送戳一戳，群聊时传 group_id，不传则为私聊戳一戳

    Args:
        msg (SEND_POKE): 戳一戳参数

    Returns:
        dict|str: 操作结果
    """
    params = msg.model_dump(exclude_none=True)
    if msg.group_id:
        return _call_api("group_poke", params)
    return _call_api("friend_poke", params)