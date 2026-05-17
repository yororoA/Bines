from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from utils import langchain_model
from thinking_settings import thinking_settings
from workflow.state import AgentRoute


_manager_model = None


def _get_manager_model():
    global _manager_model
    if _manager_model is None:
        _manager_model = langchain_model(
            model=thinking_settings.MODEL_SELECTED,
            temperature=0.3,
        ).with_structured_output(AgentRoute)
    return _manager_model


MANAGER_SYSTEM_PROMPT = """你是一个路由决策助手。你的职责是分析用户输入并决定下一步操作。

决策规则：
1. **reply** - 当用户只是闲聊、问候、提问（不需要实际操作或状态更新）时，直接回复。
2. **tool_agent** - 当用户需要执行实际操作（查看屏幕、打开应用、搜索、控制音乐、自动化操作等）时，路由到工具代理。
3. **summary_agent** - 当对话中明确发生了状态变化（地点变化、好感度变化、获得/失去物品、任务进展、NPC状态变化等）时，路由到摘要代理。

重要：
- 如果用户询问过去的对话或记忆，选择 reply（上下文中已包含记忆信息）。
- 如果用户需要实际操作 AND 状态也发生了变化，优先选择 tool_agent（状态更新可在操作完成后处理）。
- 不要拒绝合理的工具请求。
- 禁止代码/编程相关请求，选择 reply 并在 next_purpose 中说明需要拒绝。"""


def manager_node(state: dict) -> dict:
    model = _get_manager_model()

    messages_for_routing = [
        SystemMessage(content=MANAGER_SYSTEM_PROMPT),
    ]

    existing_messages = state.get("messages", [])
    for msg in existing_messages:
        if isinstance(msg, HumanMessage):
            messages_for_routing.append(HumanMessage(content=msg.content))
        elif isinstance(msg, AIMessage):
            messages_for_routing.append(AIMessage(content=msg.content))

    dynamic_text = state.get("dynamic_memory_text", "")
    if dynamic_text:
        messages_for_routing.append(
            HumanMessage(content=f"当前动态状态：\n{dynamic_text}")
        )

    user_input = state.get("user_input", "")
    messages_for_routing.append(
        HumanMessage(content=f"用户输入：{user_input}")
    )

    tool_round_count = state.get("tool_round_count", 0)
    if tool_round_count >= 5:
        route = AgentRoute(
            next_node="reply",
            reasoning=f"已执行 {tool_round_count} 轮工具调用，强制回复",
            next_purpose="",
        )
    else:
        try:
            route = model.invoke(messages_for_routing)
        except Exception as e:
            print(f"[Manager] Routing error: {e}")
            route = AgentRoute(
                next_node="reply",
                reasoning=f"路由决策失败: {e}，默认直接回复",
                next_purpose="",
            )

    print(f"[Manager] Route: {route.next_node} | Reasoning: {route.reasoning} | Purpose: {route.next_purpose}")

    return {
        "next_node": route.next_node,
        "reasoning": route.reasoning,
        "next_purpose": route.next_purpose,
    }
