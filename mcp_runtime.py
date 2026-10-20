"""Configured MCP tool discovery/calls via the official SDK; no eager dependency."""
import asyncio
import json
import os
from pathlib import Path
from common.env import load_env

load_env()

def configurations():
    path = Path(os.environ.get("BINES_MCP_CONFIG", str(Path(__file__).parent / "mcp.servers.json")))
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data.get("mcpServers", {})


async def invoke(server, name=None, arguments=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from contextlib import AsyncExitStack
    cfg = configurations().get(server)
    if not cfg or cfg.get("disabled"):
        raise ValueError("MCP server 未配置或已禁用")
    async with asyncio.timeout(float(cfg.get("timeout", 60))):
        async with AsyncExitStack() as stack:
            if "command" in cfg:
                env = dict(os.environ)
                env.update({key: os.path.expandvars(str(value)) for key, value in cfg.get("env", {}).items()})
                transport = stdio_client(StdioServerParameters(command=cfg["command"], args=cfg.get("args", []), env=env))
            else:
                from mcp.client.streamable_http import streamable_http_client
                import httpx2
                client = await stack.enter_async_context(httpx2.AsyncClient(
                    headers={key: os.path.expandvars(value) for key, value in cfg.get("headers", {}).items()},
                    timeout=float(cfg.get("timeout", 60)), follow_redirects=False,
                ))
                transport = streamable_http_client(cfg["url"], http_client=client)
            streams = await stack.enter_async_context(transport)
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            if name is None:
                listing = await session.list_tools()
                tools = list(listing.tools)
                while listing.next_cursor:
                    from mcp.types import PaginatedRequestParams
                    listing = await session.list_tools(params=PaginatedRequestParams(cursor=listing.next_cursor))
                    tools.extend(listing.tools)
                return [tool.model_dump(by_alias=True, mode="json") for tool in tools]
            result = await session.call_tool(name, arguments or {})
            return result.model_dump(by_alias=True, mode="json")


def run(server, name=None, arguments=None):
    # Synchronous SkillRunner contract; never replay failed remote writes.
    from concurrent.futures import ThreadPoolExecutor
    def execute():
        return asyncio.run(invoke(server, name, arguments))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return execute()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(execute).result()
