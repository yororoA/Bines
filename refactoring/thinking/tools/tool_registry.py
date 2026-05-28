from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ToolCategory = str

PERFORMER_TOOLS = "performer"
REPLY_TOOLS = "reply"
COMMON_TOOLS = "common"


class ToolRegistry:
    def __init__(self):
        self._tools: dict[ToolCategory, list[Any]] = {
            PERFORMER_TOOLS: [],
            REPLY_TOOLS: [],
            COMMON_TOOLS: [],
        }
        self._authorized_imports: dict[ToolCategory, set[str]] = {
            PERFORMER_TOOLS: set(),
            REPLY_TOOLS: set(),
            COMMON_TOOLS: set(),
        }
        self._discovered: bool = False
        self._defaults_registered: bool = False

    def register_tool(
        self,
        category: ToolCategory,
        tool: Any,
        authorized_imports: list[str] | None = None,
    ):
        if category not in self._tools:
            self._tools[category] = []
        if tool is not None:
            self._tools[category].append(tool)
        if authorized_imports:
            if category not in self._authorized_imports:
                self._authorized_imports[category] = set()
            self._authorized_imports[category].update(authorized_imports)

    def get_tools(self, category: ToolCategory) -> list[Any]:
        if not self._discovered:
            self._discover()
        return list(self._tools.get(category, []))

    def get_authorized_imports(self, category: ToolCategory) -> list[str]:
        if not self._discovered:
            self._discover()
        return sorted(self._authorized_imports.get(category, set()))

    def _discover(self):
        self._discovered = True
        register_default_tools()


_tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _tool_registry


def register_default_tools():
    from tools.performer_tools.webSearch import webSearch, get_search_tools, SEARCH_AUTHORIZED_IMPORTS
    from tools.napcat_tools.common_msgs.cmsg_tools import send_msg

    registry = get_tool_registry()
    if registry._defaults_registered:
        return
    registry._defaults_registered = True

    registry.register_tool(PERFORMER_TOOLS, webSearch, SEARCH_AUTHORIZED_IMPORTS)
    for tool in get_search_tools():
        registry.register_tool(PERFORMER_TOOLS, tool)
    registry.register_tool(REPLY_TOOLS, send_msg)