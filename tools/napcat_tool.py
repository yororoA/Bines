"""Complete NapCat API access, runtime schema discovery and receipt lookup."""
from chatBot.gateway_client import GatewayClient


def _destination(group_id, user_id):
    from tools.context import CURRENT_CONTEXT
    identity = CURRENT_CONTEXT.get().identity
    if group_id and user_id:
        raise ValueError("group_id 和 user_id 只能指定一个")
    if group_id:
        return "group", {"group_id": str(group_id)}
    if user_id:
        return "private", {"user_id": str(user_id)}
    if not identity.local:
        return ("group", {"group_id": identity.group}) if identity.group else ("private", {"user_id": identity.user})
    raise ValueError("本机调用必须指定 group_id 或 user_id")


def napcat_send(message: str | list | dict, group_id: str = "", user_id: str = "",
                reply_to: str | None = None, operation_id: str | None = None):
    """一次发送文本/图片/表情组合；默认当前会话，可用 reply_to 引用原消息。"""
    from chatBot.messages import outgoing_segments
    destination, params = _destination(group_id, user_id)
    # Plain text stays literal. Rich messages use explicit structured segments.
    chain = outgoing_segments(message, auto_escape=isinstance(message, str))
    if reply_to is not None:
        chain = [{"type": "reply", "data": {"id": str(reply_to)}}, *chain]
    return GatewayClient().raw_api("send_"+destination+"_msg", {**params, "message": chain},
                                   operation_id=operation_id)


def napcat_forward(message_ids: list, group_id: str = "", user_id: str = "",
                   operation_id: str | None = None):
    """将选中的 message_id 按给定顺序合并转发为一条消息；默认当前会话。"""
    if not isinstance(message_ids, list) or not message_ids:
        raise ValueError("message_ids 必须是非空列表")
    if any(not isinstance(item, (str, int)) or isinstance(item, bool) for item in message_ids):
        raise ValueError("message_ids 只能包含真实消息编号")
    destination, params = _destination(group_id, user_id)
    nodes = [{"type": "node", "data": {"id": str(message_id)}} for message_id in message_ids]
    return GatewayClient().raw_api("send_"+destination+"_forward_msg", {**params, "messages": nodes},
                                   operation_id=operation_id)


def napcat_discover(query: str = "", action: str | None = None):
    """搜索全部 NapCat 能力；返回参数 schema、来源及运行时支持确认状态。"""
    return GatewayClient().discover(query, action)


def napcat_catalog():
    """查询官方 API 数量、本身份可用数量、运行时版本目录差异与连接状态。"""
    return GatewayClient().request("/catalog", {})


def napcat_call(action: str, params: dict, operation_id: str | None = None, timeout: float = 30):
    """调用任意 NapCat action（含全部扩展 API），写操作回执可查询。"""
    return GatewayClient().raw_api(action, params, timeout, operation_id)


def napcat_operation(operation_id: str):
    """查询已执行或结果不确定的 NapCat 操作；不可换编号重发。"""
    return GatewayClient().request("/operation", {"operation_id": operation_id})


def napcat_events(limit: int = 20):
    """读取当前身份可见的消息、通知、撤回和请求原始事件（含审批 flag）。"""
    return GatewayClient().request("/events", {"limit": limit})
