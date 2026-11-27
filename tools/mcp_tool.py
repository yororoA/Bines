from mcp_runtime import configurations, run
from tools.context import CURRENT_CONTEXT


def _authorize():
    if not CURRENT_CONTEXT.get().identity.privileged:
        raise PermissionError("外部 MCP 仅允许本机或主人私聊；QQ 会话使用受限 NapCat 网关")


def mcp_discover(server: str = ""):
    """列出配置的 MCP server；指定 server 返回全部工具及参数 schema。"""
    _authorize()
    return run(server) if server else list(configurations())


def mcp_call(server: str, tool: str, arguments: dict):
    """调用已配置 MCP 工具；传输超时可能已执行，不能自动重试。"""
    _authorize()
    return run(server, tool, arguments)
