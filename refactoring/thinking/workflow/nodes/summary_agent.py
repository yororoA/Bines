from langchain_core.messages import AIMessage

from utils.thinking_helper import ThinkingModelHelper
from tools import get_summary_agent_schemas, get_tool_call_map_for_schemas


_thinking_helper = None


def _get_thinking_helper():
    global _thinking_helper
    if _thinking_helper is None:
        _thinking_helper = ThinkingModelHelper(use_thinking=False)
    return _thinking_helper


SUMMARY_AGENT_SYSTEM_PROMPT = """你负责根据对话内容更新角色扮演世界的动态状态（地点、关系、物品、任务、NPC、记忆亮点等）。
仅调用 update_status 工具，且只传入有变化的字段；无需更新的不要传。
若对话中无任何状态变化，可不调用工具。
【关系/好感度更新】若对话中出现关系或好感度变化，必须传入 relationship_delta（整数）：正数提升好感，负数降低。relationship_level/relationship_score/relationship_distribution 由系统根据 delta 自动计算，不要传 relationship_level。"""


def summary_agent_node(state: dict) -> dict:
    helper = _get_thinking_helper()
    schemas = get_summary_agent_schemas()
    tool_call_map = get_tool_call_map_for_schemas(schemas)

    state_update_description = state.get("next_purpose", "")
    if not state_update_description:
        state_update_description = state.get("user_input", "")

    dynamic_json = state.get("dynamic_memory_json", "")

    messages = [
        {"role": "system", "content": SUMMARY_AGENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"【当前 memory_dynamic.json 内容（用于判断需要更新哪些字段）】\n{dynamic_json}\n\n"
                f"【状态变化描述】\n{state_update_description.strip()}\n\n"
                f"请根据描述更新动态记忆（仅输出有变化的字段）；若有关系/好感度变化请务必传入 relationship_delta（整数）。"
            ),
        },
    ]

    try:
        final_content, tool_history, reasoning_contents = helper.run_tool_calling_turn(
            messages=messages,
            tools=schemas,
            tool_call_map=tool_call_map,
            turn=1,
            task_goal="根据对话更新动态记忆",
        )

        result = final_content or "状态更新完成"
        if tool_history:
            print(f"[SummaryAgent] Updated: {[t.get('tool_name') for t in tool_history]}")

        return {
            "state_update_result": result,
            "messages": [AIMessage(content=f"[SummaryAgent] {result[:200]}")],
        }
    except Exception as e:
        print(f"[SummaryAgent] Error: {e}")
        import traceback
        traceback.print_exc()
        return {
            "state_update_result": f"状态更新出错：{e}",
        }
