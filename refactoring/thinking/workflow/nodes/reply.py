from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from utils import langchain_model
from thinking_settings import thinking_settings


_reply_model = None


def _get_reply_model():
    global _reply_model
    if _reply_model is None:
        _reply_model = langchain_model(
            model=thinking_settings.MODEL_SELECTED,
            temperature=0.7,
        )
    return _reply_model


def reply_node(state: dict) -> dict:
    model = _get_reply_model()

    system_prompt = state.get("system_prompt", "")
    messages = list(state.get("messages", []))

    llm_messages = [SystemMessage(content=system_prompt)]
    for msg in messages:
        if isinstance(msg, (HumanMessage, AIMessage)):
            llm_messages.append(msg)

    tool_result = state.get("tool_result")
    if tool_result:
        llm_messages.append(
            HumanMessage(content=f"[工具执行结果]（仅内部参考，不要照搬，保持角色人设回复）：\n{tool_result}")
        )

    state_update_result = state.get("state_update_result")
    if state_update_result:
        llm_messages.append(
            HumanMessage(content=f"[状态更新结果]（仅内部参考）：\n{state_update_result}")
        )

    try:
        response = model.invoke(llm_messages)
        reply_text = response.content
    except Exception as e:
        print(f"[Reply] Error: {e}")
        reply_text = f"……抱歉，出了点小问题。  {e}"

    print(f"[Reply] Generated: {reply_text[:100]}...")

    return {
        "reply_text": reply_text,
        "final_reply": reply_text,
        "messages": [AIMessage(content=reply_text)],
    }
