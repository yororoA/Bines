from smolagents import tool
from napcat_server import napcat_client
from .types import SEND_MSG


@tool("send qq message")
def send_msg(msg: SEND_MSG) -> dict | str:
    """
    通过 QQ 发送消息

    Args:
        msg (SEND_MSG): 要发送的消息

    Returns:
        dict|str: 发送结果
    """
    if napcat_client:
        try:
            import asyncio
            res = asyncio.run(napcat_client.call_api(
                action="send_msg",
                params=msg.model_dump(),
            ))
            return res
        except Exception as e:
            return f"Error sending message: {e}"

