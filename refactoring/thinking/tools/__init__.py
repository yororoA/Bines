import datetime
from copy import deepcopy
from typing import Any, Callable, Dict, List

from memory import DynamicMemory

_dynamic_memory: DynamicMemory | None = None


def set_dynamic_memory(dm: DynamicMemory):
    global _dynamic_memory
    _dynamic_memory = dm


def _get_dynamic_memory() -> DynamicMemory:
    if _dynamic_memory is None:
        _dynamic_memory = DynamicMemory()
    return _dynamic_memory


def get_time() -> str:
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


def task_complete(result: str = "") -> str:
    return result if result else "任务完成"


def update_status(**kwargs) -> str:
    dm = _get_dynamic_memory()
    return dm.update_status(**kwargs)


TOOLS_REGISTRY: Dict[str, Callable] = {
    "get_time": get_time,
    "task_complete": task_complete,
    "update_status": update_status,
}


def call_tool(name: str, **kwargs) -> Any:
    fn = TOOLS_REGISTRY.get(name)
    if fn is None:
        raise ValueError(f"Tool '{name}' not found in registry")
    return fn(**kwargs)


_GET_TIME_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "获取当前系统时间和日期",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_TASK_COMPLETE_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task_complete",
        "description": "任务完成时调用，提交最终结果并结束工具调用循环",
        "parameters": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "任务的最终结果描述",
                }
            },
            "required": [],
        },
    },
}

_UPDATE_STATUS_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_status",
        "description": "根据对话内容更新当前角色扮演世界中的动态状态，包括地点、好感度、背包物品、当前任务、NPC状态和记忆亮点等。仅输出有变化的字段。",
        "parameters": {
            "type": "object",
            "properties": {
                "current_time": {"type": "string", "description": "当前时间戳（ISO格式）"},
                "current_location": {"type": "string", "description": "当前所在地点或场景"},
                "relationship_delta": {
                    "type": "integer",
                    "description": "关系增量（正数提升好感，负数降低好感）；relationship_level/score/distribution 由系统根据 delta 自动计算",
                },
                "add_item": {"type": "string", "description": "获得的新物品名称"},
                "remove_item": {"type": "string", "description": "失去或消耗的物品名称"},
                "active_quest": {"type": "string", "description": "当前正在进行的任务或目标描述"},
                "npc_name": {"type": "string", "description": "更新NPC的名称"},
                "npc_attire": {"type": "string", "description": "更新NPC的服装描述"},
                "npc_visual_status": {"type": "string", "description": "更新视觉模块的状态"},
                "npc_activity": {"type": "string", "description": "更新NPC当前正在进行的活动"},
                "add_memory_highlight": {"type": "string", "description": "添加一条记忆亮点"},
                "remove_memory_highlight": {"type": "string", "description": "移除一条记忆亮点"},
                "important_thing": {"type": "string", "description": "当前重要的人/事/物（一句话描述）"},
            },
            "required": [],
        },
    },
}


def get_all_tool_schemas() -> List[Dict[str, Any]]:
    return deepcopy([_GET_TIME_SCHEMA, _TASK_COMPLETE_SCHEMA, _UPDATE_STATUS_SCHEMA])


def get_tool_agent_schemas() -> List[Dict[str, Any]]:
    return deepcopy([_GET_TIME_SCHEMA, _TASK_COMPLETE_SCHEMA])


def get_summary_agent_schemas() -> List[Dict[str, Any]]:
    return deepcopy([_UPDATE_STATUS_SCHEMA])


def get_tool_call_map_for_schemas(schemas: List[Dict[str, Any]]) -> Dict[str, Callable]:
    names = {s.get("function", {}).get("name") for s in schemas}
    return {k: v for k, v in TOOLS_REGISTRY.items() if k in names}
