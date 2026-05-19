import asyncio
from napcat_server import napcat_client, NapCatClient
from thinking_settings import thinking_settings


async def main():
    # 连接napcat
    global napcat_client
    napcat_client = NapCatClient(
        thinking_settings.NAPCAT_WS_API_URL, thinking_settings.NAPCAT_WS_API_TOKEN
    )
    try:
        con_task = asyncio.create_task(napcat_client.connect())
        await asyncio.sleep(2)
        await con_task
    except asyncio.CancelledError:
        print("\nShutting down...")
    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        if napcat_client:
            await napcat_client.close()


# 启动graph以及连接napcat
if __name__ == "__main__":
    asyncio.run(main())
