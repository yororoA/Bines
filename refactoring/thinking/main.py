import asyncio
import logging
from datetime import datetime, timedelta

from napcat_server import NapCatClient
from thinking_settings import thinking_settings

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)

logger = logging.getLogger(__name__)


def _run_startup_buffer_consolidation():
    try:
        from memory import (
            get_buffer_by_day,
            get_existing_diary_day_keys,
            consolidate_buffer_to_diary,
        )
        from utils.time_utils import day_key

        now = datetime.now()
        yesterday = now - timedelta(days=1)
        yesterday_key = day_key(yesterday)

        existing_days = get_existing_diary_day_keys()
        if yesterday_key in existing_days:
            return

        buffer_entries = get_buffer_by_day(yesterday_key)
        if not buffer_entries:
            return

        logger.info("Consolidating buffer for %s (%d entries)", yesterday_key, len(buffer_entries))
        result = consolidate_buffer_to_diary(yesterday_key)
        if result:
            logger.info("Startup buffer consolidation completed for %s", yesterday_key)
    except Exception:
        logger.exception("Startup buffer consolidation failed")


async def main():
    import napcat_server.global_client as gc

    napcat_client = NapCatClient(
        thinking_settings.NAPCAT_WS_SERVER, thinking_settings.NAPCAT_WS_TOKEN
    )
    gc.napcat_client = napcat_client
    _run_startup_buffer_consolidation()
    try:
        await napcat_client.process_messages()
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    except Exception:
        logger.exception("Unexpected error")
    finally:
        if napcat_client:
            await napcat_client.close()


if __name__ == "__main__":
    asyncio.run(main())
