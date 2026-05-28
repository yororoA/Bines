import asyncio

from smolagents import tool
from napcat_server.global_client import get_client
from .types import SEND_MSG
from thinking_settings import thinking_settings


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            coro, loop
        )
        return future.result(
            timeout=thinking_settings.NAPCAT_WS_API_RESPONSE_TIMEOUT + 5
        )
    return asyncio.run(coro)


@tool("send qq message")
def send_msg(msg: SEND_MSG) -> dict | str:
    """
    通过 QQ 发送消息

    Args:
        msg (SEND_MSG): 要发送的消息

    Returns:
        dict|str: 发送结果
    """
    client = get_client()
    if not client:
        return {"error": "NapCat client is not connected"}
    try:
        res = _run_async(client.call_api(
            action="send_msg",
            params=msg.model_dump(),
        ))
        return res
    except Exception as e:
        return {"error": f"Error sending message: {e}"}

