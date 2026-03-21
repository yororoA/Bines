import copy
import json
from pathlib import Path
from typing import Any, Dict, List


def bootstrap_tool_agent_schema_if_missing(
    schema_path: Path,
    all_tools_schema: List[Dict[str, Any]],
) -> None:
    """若 schema 文件不存在，则从代码中的工具 schema 生成初始文件（全部 enabled=true）。"""
    if schema_path.exists():
        return
    try:
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        tools = sorted(
            [
                {
                    "name": t.get("function", {}).get("name", ""),
                    "description": (t.get("function") or {}).get("description", ""),
                    "enabled": True,
                }
                for t in all_tools_schema
            ],
            key=lambda x: x.get("name", ""),
        )
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump({"tools": tools}, f, ensure_ascii=False, indent=2)
        print(f"[Thinking] 已生成初始 tool_agent_schema.json（共 {len(tools)} 项，全部启用）", flush=True)
    except Exception as e:
        print(f"[Thinking] 生成 tool_agent_schema.json 失败: {e}", flush=True)


def get_tool_agent_schema_filtered(
    schema_path: Path,
    all_tools_schema: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    从 server/tool_agent_schema.json 读取 tools[].enabled，
    仅挂载 enabled=true 的工具（与 all_tools_schema 按 name 对齐）。
    对 `sing` 动态注入 `filename` 枚举。
    """
    bootstrap_tool_agent_schema_if_missing(schema_path, all_tools_schema)
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[Thinking] 读取 tool_agent_schema.json 失败: {e}，将使用全部工具", flush=True)
        return list(all_tools_schema)

    tools_conf = data.get("tools") or []
    names_in_json = {str(t.get("name")) for t in tools_conf}
    enabled_names = {str(t.get("name")) for t in tools_conf if t.get("enabled") is True}
    code_names = {t.get("function", {}).get("name") for t in all_tools_schema}

    # 代码中有但 JSON 中未列出的工具默认启用，避免新增工具时还要手动补 JSON。
    enabled_names = enabled_names | (code_names - names_in_json)

    missing = enabled_names - code_names
    if missing:
        print(f"[Thinking] schema 中启用的工具在代码中不存在，已忽略: {missing}", flush=True)

    if not enabled_names:
        return []

    filtered = [
        t for t in all_tools_schema
        if t.get("function", {}).get("name") in enabled_names
    ]
    result = copy.deepcopy(filtered)

    for t in result:
        if t.get("function", {}).get("name") == "sing":
            try:
                from tools.sing_tool import get_sing_list
                choices = get_sing_list()
            except Exception:
                choices = []
            params = (t.get("function") or {}).get("parameters") or {}
            props = params.get("properties") or {}
            if "filename" in props and choices:
                props["filename"] = {**props["filename"], "enum": choices}
            break

    return result
