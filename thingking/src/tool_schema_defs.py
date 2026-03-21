from copy import deepcopy
from typing import Any, Dict


_GET_TIME_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "获取当前系统时间和日期 (Get current system time)",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


_BROWSER_SEARCH_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "browser_search",
        "description": "与浏览器相关的唯一入口：仅允许通过本工具（浏览器自动化/Selenium）进行浏览器操作，不依赖屏幕坐标。启动浏览器并执行网页搜索，返回搜索结果文本。当任务涉及「打开浏览器」「用浏览器搜索」「在网上搜一下」「查一下」等时必须调用本工具。禁止用 automate_action/automate_sequence 或屏幕坐标操作浏览器。Execute web search via browser (Selenium) only; no screen coordinates. MUST use for any browser-related task. Do NOT use automate_action, automate_sequence or coordinates for browser.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。Search query.",
                }
            },
            "required": ["query"],
        },
    },
}


def get_time_tool_schema() -> Dict[str, Any]:
    return deepcopy(_GET_TIME_TOOL_SCHEMA)


def browser_search_tool_schema() -> Dict[str, Any]:
    return deepcopy(_BROWSER_SEARCH_TOOL_SCHEMA)
