"""Explicit Ark identity/chat/recovery actions; no autonomous scheduling."""
from ark_mcp_client import call_ark


def get_ark_profile() -> dict:
    """读取 Bines 的站内 UID、资料、权限与在线状态。"""
    return call_ark("ark_get_profile")


def get_ark_conversations() -> dict:
    """列出 Bines 可访问的会话：group（群聊）、admin（与管理员私聊）。"""
    return call_ark("ark_list_conversations")


def get_ark_chat_history(conversation: str, page: int = 1, limit: int = 20) -> dict:
    """按页读取 group 或 admin，返回最新消息、发言人 UID 与回复引用。"""
    return call_ark("ark_chat_history", {"conversation": conversation, "page": page, "limit": limit})


def send_ark_chat(conversation: str, text: str, reply_to: str = "", request_id: str | None = None) -> dict:
    """仅在用户明确要求时发消息。失败后用原 request_id 查询，禁止换编号重发。"""
    return call_ark("ark_send_chat", {
        "conversation": conversation, "text": text, "reply_to": reply_to, "request_id": request_id,
    }, write=True)


def get_ark_operation(request_id: str) -> dict:
    """查询原写入：completed 返回原结果；processing/unknown 请等待或核查，勿重发。"""
    return call_ark("ark_get_operation", {"request_id": request_id})


ARK_TOOLS = {
    "get_ark_profile": get_ark_profile,
    "get_ark_conversations": get_ark_conversations,
    "get_ark_chat_history": get_ark_chat_history,
    "send_ark_chat": send_ark_chat,
    "get_ark_operation": get_ark_operation,
}
