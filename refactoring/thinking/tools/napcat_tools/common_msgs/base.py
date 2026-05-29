import asyncio
import logging

from napcat_server.global_client import get_client
from thinking_settings import thinking_settings

logger = logging.getLogger(__name__)


def _run_async(coro):
    client = get_client()
    main_loop = client.get_main_loop() if client else None
    if main_loop and main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, main_loop)
        return future.result(
            timeout=thinking_settings.NAPCAT_WS_API_RESPONSE_TIMEOUT + 5
        )
    return asyncio.run(coro)


def _call_api(action: str, params: dict | None = None) -> dict:
    client = get_client()
    if not client:
        return {"error": "NapCat client is not connected"}
    try:
        res = _run_async(client.call_api(action=action, params=params or {}))
        return res
    except Exception as e:
        logger.exception("NapCat API call failed: %s", action)
        return {"error": f"Error calling {action}: {e}"}