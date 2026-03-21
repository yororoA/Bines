import inspect
from typing import Any, Dict, Iterable, List, Set


def _anno_to_json_type(annotation: Any) -> str:
    if annotation in (int,):
        return "integer"
    if annotation in (float,):
        return "number"
    if annotation in (bool,):
        return "boolean"
    if annotation in (list, tuple, set):
        return "array"
    if annotation in (dict,):
        return "object"
    return "string"


def _build_min_schema(tool_name: str, func) -> Dict[str, Any]:
    try:
        signature = inspect.signature(func)
    except Exception:
        signature = None

    properties: Dict[str, Dict[str, Any]] = {}
    required: List[str] = []

    if signature is not None:
        for name, param in signature.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            json_type = _anno_to_json_type(param.annotation)
            properties[name] = {
                "type": json_type,
                "description": f"Parameter: {name}",
            }
            if param.default is inspect.Parameter.empty:
                required.append(name)

    doc = inspect.getdoc(func) or ""
    first_line = doc.splitlines()[0].strip() if doc else ""
    description = first_line or f"Invoke tool: {tool_name}."

    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def sync_agent_schema_with_registry(
    base_schema: List[Dict[str, Any]],
    tools_registry: Dict[str, Any],
    excluded_names: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """
    用注册表补齐 schema 中缺失的工具，避免新增工具时必须手动双改。
    """
    excluded: Set[str] = set(excluded_names or ())
    merged = list(base_schema)
    existing = {
        (item.get("function") or {}).get("name")
        for item in merged
        if isinstance(item, dict)
    }

    for tool_name, func in tools_registry.items():
        if tool_name in excluded:
            continue
        if tool_name in existing:
            continue
        merged.append(_build_min_schema(tool_name, func))

    return merged
