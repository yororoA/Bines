from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

ToolCategory = str

PERFORMER_TOOLS = "performer"
REPLY_TOOLS = "reply"


class ToolRegistry:
    def __init__(self):
        self._tools: dict[ToolCategory, list[Any]] = {
            PERFORMER_TOOLS: [],
            REPLY_TOOLS: [],
        }
        self._authorized_imports: dict[ToolCategory, set[str]] = {
            PERFORMER_TOOLS: set(),
            REPLY_TOOLS: set(),
        }
        self._discovered: bool = False
        self._defaults_registered: bool = False
        self._lock = threading.Lock()

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
            with self._lock:
                if not self._discovered:
                    self._discover()
                    self._discovered = True
        return list(self._tools.get(category, []))

    def get_authorized_imports(self, category: ToolCategory) -> list[str]:
        if not self._discovered:
            with self._lock:
                if not self._discovered:
                    self._discover()
                    self._discovered = True
        return sorted(self._authorized_imports.get(category, set()))

    def _discover(self):
        register_default_tools()


_tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _tool_registry


def register_default_tools():
    from tools.performer_tools.webSearch import webSearch, get_search_tools, SEARCH_AUTHORIZED_IMPORTS
    from tools.performer_tools.visualRecognition import visualRecognition, VISUAL_RECOGNITION_AUTHORIZED_IMPORTS
    from tools.napcat_tools.common_msgs.cmsg_tools import send_msg
    from tools.napcat_tools.common_msgs.msg_tools import (
        delete_msg,
        get_msg,
        send_forward_msg,
        send_group_forward_msg,
        send_private_forward_msg,
        get_group_msg_history,
        get_friend_msg_history,
    )
    from tools.napcat_tools.common_msgs.group_tools import (
        get_group_list,
        get_group_info,
        get_group_member_list,
        get_group_member_info,
    )
    from tools.napcat_tools.common_msgs.interact_tools import send_poke

    registry = get_tool_registry()
    if registry._defaults_registered:
        return
    registry._defaults_registered = True

    registry.register_tool(PERFORMER_TOOLS, webSearch, SEARCH_AUTHORIZED_IMPORTS)
    registry.register_tool(PERFORMER_TOOLS, visualRecognition, VISUAL_RECOGNITION_AUTHORIZED_IMPORTS)
    registry.register_tool(PERFORMER_TOOLS, send_msg)
    registry.register_tool(PERFORMER_TOOLS, delete_msg)
    registry.register_tool(PERFORMER_TOOLS, get_msg)
    registry.register_tool(PERFORMER_TOOLS, send_forward_msg)
    registry.register_tool(PERFORMER_TOOLS, send_group_forward_msg)
    registry.register_tool(PERFORMER_TOOLS, send_private_forward_msg)
    registry.register_tool(PERFORMER_TOOLS, get_group_msg_history)
    registry.register_tool(PERFORMER_TOOLS, get_friend_msg_history)
    registry.register_tool(PERFORMER_TOOLS, get_group_list)
    registry.register_tool(PERFORMER_TOOLS, get_group_info)
    registry.register_tool(PERFORMER_TOOLS, get_group_member_list)
    registry.register_tool(PERFORMER_TOOLS, get_group_member_info)
    registry.register_tool(PERFORMER_TOOLS, send_poke)

    registry.register_tool(REPLY_TOOLS, send_msg)