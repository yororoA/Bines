import asyncio
import logging

from napcat_server import NapCatClient
from thinking_settings import thinking_settings

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)

logger = logging.getLogger(__name__)


async def main():
    import napcat_server.global_client as gc

    napcat_client = NapCatClient(
        thinking_settings.NAPCAT_WS_SERVER, thinking_settings.NAPCAT_WS_TOKEN
    )
    gc.napcat_client = napcat_client
    stop_event = asyncio.Event()
    try:
        con_task = asyncio.create_task(napcat_client.connect())
        await con_task
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    except Exception:
        logger.exception("Unexpected error")
    finally:
        if napcat_client:
            await napcat_client.close()


if __name__ == "__main__":
    asyncio.run(main())
