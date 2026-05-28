from smolagents import tool
from .base import _call_api


@tool("get group list")
def get_group_list() -> dict | str:
    """
    获取当前机器人加入的所有群列表

    Returns:
        dict|str: 群组列表
    """
    return _call_api("get_group_list")


@tool("get group info")
def get_group_info(group_id: str) -> dict | str:
    """
    获取指定群的详细信息

    Args:
        group_id (str): 群号

    Returns:
        dict|str: 群信息
    """
    return _call_api("get_group_info", {"group_id": group_id})


@tool("get group member list")
def get_group_member_list(group_id: str) -> dict | str:
    """
    获取指定群的成员列表

    Args:
        group_id (str): 群号

    Returns:
        dict|str: 群成员列表
    """
    return _call_api("get_group_member_list", {"group_id": group_id})


@tool("get group member info")
def get_group_member_info(group_id: str, user_id: str) -> dict | str:
    """
    获取指定群成员的详细信息

    Args:
        group_id (str): 群号
        user_id (str): 用户QQ号

    Returns:
        dict|str: 群成员详细信息
    """
    return _call_api("get_group_member_info", {
        "group_id": group_id,
        "user_id": user_id,
    })