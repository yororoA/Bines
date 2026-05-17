from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage

from utils.thinking_helper import ThinkingModelHelper
from tools import get_tool_agent_schemas, get_tool_call_map_for_schemas


_thinking_helper = None


def _get_thinking_helper():
    global _thinking_helper
    if _thinking_helper is None:
        _thinking_helper = ThinkingModelHelper(use_thinking=False)
    return _thinking_helper


TOOL_AGENT_SYSTEM_PROMPT = """你需要执行以下任务。请根据任务需要调用工具。可自主连续多轮调用工具，根据结果再决定是否继续调用或调用 task_complete 提交最终结果。

【重要约束】你不具备访问对话历史或记忆的能力。如果任务要求检索过去的对话内容，请直接调用 task_complete 并回复"历史对话信息已在主模型上下文中，无需工具操作"。"""


def tool_agent_node(state: dict) -> dict:
    helper = _get_thinking_helper()
    schemas = get_tool_agent_schemas()
    tool_call_map = get_tool_call_map_for_schemas(schemas)

    task_description = state.get("next_purpose", "")
    if not task_description:
        task_description = state.get("user_input", "")

    messages = [
        {"role": "system", "content": TOOL_AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"任务：{task_description}"},
    ]

    try:
        final_content, tool_history, reasoning_contents = helper.run_tool_calling_turn(
            messages=messages,
            tools=schemas,
            tool_call_map=tool_call_map,
            turn=1,
            task_goal=task_description,
            max_iterations=25,
        )

        result = final_content or "任务执行完成"
        if reasoning_contents:
            reasoning_summary = "\n\n[工具大模型的思考过程]：" + "\n".join(reasoning_contents)
            result = result + reasoning_summary

        print(f"[ToolAgent] Task done: {task_description[:50]}... | Tools used: {len(tool_history)}")

        current_count = state.get("tool_round_count", 0)

        return {
            "tool_result": result,
            "tool_history": tool_history,
            "tool_round_count": current_count + 1,
            "messages": [AIMessage(content=f"[ToolAgent] {result[:200]}")],
        }
    except Exception as e:
        print(f"[ToolAgent] Error: {e}")
        import traceback
        traceback.print_exc()
        return {
            "tool_result": f"工具执行出错：{e}",
            "tool_round_count": state.get("tool_round_count", 0) + 1,
        }
