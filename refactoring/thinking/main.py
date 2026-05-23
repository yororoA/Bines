import asyncio
from napcat_server import NapCatClient
from thinking_settings import thinking_settings


async def main():
    import napcat_server.global_client as gc
    # 连接napcat
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
        print("\nShutting down...")
    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        if napcat_client:
            await napcat_client.close()


if __name__ == "__main__":
    asyncio.run(main())
