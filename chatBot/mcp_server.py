"""NapCat MCP stdio entry point backed by the same capability gateway."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, ListToolsResult, CallToolResult
    from chatBot.gateway_client import GatewayClient
    from chatBot.catalog import action_tool_specs, reference_catalog
    client = GatewayClient()
    action_names = {}

    async def list_tools(ctx, params):
        base = [
            Tool(name="napcat_discover", description="Discover all NapCat actions and current payload schemas",
                 inputSchema={"type": "object", "properties": {"query": {"type": "string"}, "action": {"type": "string"}}}),
            Tool(name="napcat_call", description="Call any NapCat action; writes require a stable operation_id",
                 inputSchema={"type": "object", "properties": {
                     "action": {"type": "string"}, "params": {"type": "object"},
                     "operation_id": {"type": "string"}, "timeout": {"type": "number"}},
                     "required": ["action", "params"]}),
            Tool(name="napcat_operation", description="Inspect a write receipt; never replay uncertain writes",
                 inputSchema={"type": "object", "properties": {"operation_id": {"type": "string"}},
                              "required": ["operation_id"]}),
            Tool(name="napcat_catalog", description="Report complete API coverage, permissions and runtime support",
                 inputSchema={"type": "object", "properties": {}}),
        ]
        # Runtime actions also appear as individually typed MCP tools.
        try:
            catalog = await asyncio.to_thread(client.request, "/schemas", {})
            if not isinstance(catalog, dict) or not catalog or any(not isinstance(entry, dict) for entry in catalog.values()):
                raise ValueError("Invalid gateway catalog")
        except Exception:
            catalog = reference_catalog()
        for specification in action_tool_specs(catalog):
            action_names[specification["name"]] = specification["_meta"]["napcat_action"]
            base.append(Tool(**specification))
        return ListToolsResult(tools=base)

    async def call_tool(ctx, params):
        name, arguments = params.name, params.arguments or {}
        try:
            if name == "napcat_discover":
                result = await asyncio.to_thread(client.discover, **arguments)
            elif name == "napcat_operation":
                result = await asyncio.to_thread(client.request, "/operation", arguments)
            elif name == "napcat_catalog":
                result = await asyncio.to_thread(client.request, "/catalog", {})
            elif name == "napcat_call":
                from chatBot.capabilities import is_read
                if not is_read(arguments["action"]) and not arguments.get("operation_id"):
                    raise ValueError("写操作必须提供稳定 operation_id")
                result = await asyncio.to_thread(client.raw_api, **arguments)
            elif name.startswith("napcat__"):
                if name not in action_names:
                    # Allow direct tool calls before tools/list without decoding arbitrary names.
                    for specification in action_tool_specs(reference_catalog()):
                        action_names[specification["name"]] = specification["_meta"]["napcat_action"]
                if name not in action_names:
                    raise ValueError("Unknown NapCat tool; refresh tools/list")
                action = action_names[name]
                from chatBot.capabilities import is_read
                if not is_read(action) and not arguments.get("operation_id"):
                    raise ValueError("写操作必须提供稳定 operation_id")
                result = await asyncio.to_thread(client.raw_api, action, **arguments)
            else:
                raise ValueError("Unknown tool")
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                  isError=isinstance(result, dict) and result.get("status") in {"failed", "uncertain"})
        except Exception as exc:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": str(exc)}, ensure_ascii=False))],
                                  isError=True)

    server = Server("bines-napcat", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
