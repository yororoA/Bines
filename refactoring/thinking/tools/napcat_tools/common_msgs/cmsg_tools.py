from langchain.tools import tool
from napcat_server import napcat_client
from .types import SEND_MSG


@tool
async def send_msg(msg: SEND_MSG)->dict:
    """
    发送消息

    Args:
        msg (SEND_MSG): 要发送的消息

    Returns:
        dict: 发送结果
    """
    if napcat_client:
        try:
            res = await napcat_client.call_api(
                action="send_msg",
                params=msg,
            )
            return res
        except Exception as e:
            return f"Error sending message: {e}"
