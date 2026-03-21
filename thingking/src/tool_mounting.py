from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

from tool_agent_schema import get_tool_agent_schema_filtered


def initialize_agent_tool_mounts(
    *,
    main_agent,
    tool_agent,
    router_tools_schema: List[Dict[str, Any]],
    tool_agent_schema_path: Path,
    all_tools_schema_for_agent: List[Dict[str, Any]],
) -> None:
    """初始化主模型与工具模型的工具挂载。"""
    main_agent.set_tools_schema(router_tools_schema)
    tool_agent.set_tools_schema(
        get_tool_agent_schema_filtered(tool_agent_schema_path, all_tools_schema_for_agent)
    )


def configure_tool_agent_schema_for_source(
    *,
    tool_agent,
    tool_agent_schema_path: Path,
    all_tools_schema_for_agent: List[Dict[str, Any]],
    source: str = None,
    qq_allowed_tools: Iterable[str] = (),
) -> Tuple[List[Dict[str, Any]], int, bool]:
    """
    按消息来源动态重挂工具模型 schema。
    返回 (实际挂载 schema, 全量 schema 数量, 是否 QQ 模式)。
    """
    full_schema = get_tool_agent_schema_filtered(tool_agent_schema_path, all_tools_schema_for_agent)
    if source in ("QQ", "qq"):
        allowed: Set[str] = set(qq_allowed_tools or ())
        qq_schema = [
            t
            for t in full_schema
            if (t.get("function") or {}).get("name") in allowed
        ]
        tool_agent.set_tools_schema(qq_schema)
        return qq_schema, len(full_schema), True

    tool_agent.set_tools_schema(full_schema)
    return full_schema, len(full_schema), False
