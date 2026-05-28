from smolagents import tool
from .types import (
    DELETE_MSG,
    GET_MSG,
    SEND_FORWARD_MSG,
    SEND_GROUP_FORWARD_MSG,
    SEND_PRIVATE_FORWARD_MSG,
    GET_GROUP_MSG_HISTORY,
    GET_FRIEND_MSG_HISTORY,
)
from .base import _call_api


@tool("delete qq message")
def delete_msg(msg: DELETE_MSG) -> dict | str:
    """
    撤回已发送的QQ消息

    Args:
        msg (DELETE_MSG): 包含 message_id 的参数

    Returns:
        dict|str: 操作结果
    """
    return _call_api("delete_msg", msg.model_dump())


@tool("get qq message detail")
def get_msg(msg: GET_MSG) -> dict | str:
    """
    获取QQ消息详情

    Args:
        msg (GET_MSG): 包含 message_id 的参数

    Returns:
        dict|str: 消息详情
    """
    return _call_api("get_msg", msg.model_dump())


@tool("send forward message")
def send_forward_msg(msg: SEND_FORWARD_MSG) -> dict | str:
    """
    发送合并转发消息，支持私聊和群聊

    Args:
        msg (SEND_FORWARD_MSG): 合并转发参数

    Returns:
        dict|str: 包含 message_id 和 forward_id 的结果
    """
    params = msg.model_dump(exclude_none=True)
    return _call_api("send_forward_msg", params)


@tool("send group forward message")
def send_group_forward_msg(msg: SEND_GROUP_FORWARD_MSG) -> dict | str:
    """
    发送群合并转发消息

    Args:
        msg (SEND_GROUP_FORWARD_MSG): 群合并转发参数

    Returns:
        dict|str: 包含 message_id 和 forward_id 的结果
    """
    params = msg.model_dump(exclude_none=True)
    return _call_api("send_group_forward_msg", params)


@tool("send private forward message")
def send_private_forward_msg(msg: SEND_PRIVATE_FORWARD_MSG) -> dict | str:
    """
    发送私聊合并转发消息

    Args:
        msg (SEND_PRIVATE_FORWARD_MSG): 私聊合并转发参数

    Returns:
        dict|str: 包含 message_id 和 forward_id 的结果
    """
    params = msg.model_dump(exclude_none=True)
    return _call_api("send_private_forward_msg", params)


@tool("get group message history")
def get_group_msg_history(msg: GET_GROUP_MSG_HISTORY) -> dict | str:
    """
    获取群历史消息

    Args:
        msg (GET_GROUP_MSG_HISTORY): 群消息历史参数

    Returns:
        dict|str: 消息列表
    """
    params = msg.model_dump(exclude_none=True)
    return _call_api("get_group_msg_history", params)


@tool("get friend message history")
def get_friend_msg_history(msg: GET_FRIEND_MSG_HISTORY) -> dict | str:
    """
    获取好友(私聊)历史消息

    Args:
        msg (GET_FRIEND_MSG_HISTORY): 好友消息历史参数

    Returns:
        dict|str: 消息列表
    """
    params = msg.model_dump(exclude_none=True)
    return _call_api("get_friend_msg_history", params)