import asyncio
import logging
import signal
from datetime import datetime, timedelta

from napcat_server import NapCatClient
from thinking_settings import thinking_settings

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)

logger = logging.getLogger(__name__)


def _validate_llm_provider_config() -> bool:
    """Fail fast if no LLM provider is configured for the selected model."""
    try:
        from utils.model_registry import ensure_registry_initialized, get_model_registry

        ensure_registry_initialized()
        registry = get_model_registry()
        provider = registry.find(thinking_settings.MODEL_SELECTED)
        if provider:
            return True

        names = registry.all_model_names
        logger.error(
            "No provider configured for MODEL_SELECTED=%s. "
            "Registered models=%s. Please fill thinking.env (see thinking.env.example).",
            thinking_settings.MODEL_SELECTED,
            names,
        )
        return False
    except Exception:
        logger.exception("LLM provider validation failed")
        return False


async def _run_startup_buffer_consolidation():
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

    if not _validate_llm_provider_config():
        return

    napcat_client = NapCatClient(
        thinking_settings.NAPCAT_WS_SERVER, thinking_settings.NAPCAT_WS_TOKEN
    )
    gc.napcat_client = napcat_client
    await _run_startup_buffer_consolidation()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        process_task = asyncio.create_task(napcat_client.process_messages())
        done, pending = await asyncio.wait(
            [shutdown_task, process_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    except Exception:
        logger.exception("Unexpected error")
    finally:
        from napcat_server.napcat_connection import close_workflow
        if napcat_client:
            await napcat_client.close()
        close_workflow()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
