from thinking_settings import thinking_settings
from utils import langchain_model
from utils.chroma_store import add_conversation, retrieve_relevant_str
from ..messagesState import MessagesState, AgentRoute

manager_agent = langchain_model(
    model=thinking_settings.MODEL_SELECTED
).with_structured_output(AgentRoute, method="json_mode")


def manager_node(state: MessagesState):
    from langchain_core.messages import SystemMessage

    purposed_nodes = state.get("purposed_nodes", [])
    feedbacks = state.get("purposed_feedbacks", [])
    messages = state.get("messages", [])

    user_query = ""
    for msg in reversed(messages):
        if msg.type == "human":
            user_query = msg.content
            break

    rag_context = ""
    if user_query:
        rag_context = retrieve_relevant_str(user_query)

    sys_prompt = f"""You are an intelligent workflow manager. Analyze the user request and decide the next step.

You MUST respond in valid JSON format matching this schema:
{{
    "next_node": "The next node to route to. MUST be exactly 'web_search', 'rag_search', or 'reply'.",
    "reasoning": "The reasoning behind the decision.",
    "next_purpose": "The detailed next purpose or goal that the next node should aim for.",
    "final_reply": "The final reply to the user. Must be used when next_node is 'reply'."
}}

Available nodes:
- web_search: Search the web for real-time information when the query requires up-to-date or external data.
- rag_search: Search the local knowledge base (Chroma vector store) for relevant historical conversations and context. Use this when the user's question may be answered by past interactions or stored knowledge.
- reply: Provide the final reply to the user when you have enough information to answer.
"""
    if rag_context:
        sys_prompt += f"\n\nRelevant context from knowledge base:\n{rag_context}"

    if purposed_nodes:
        sys_prompt += f"\nPrevious actions tried: {purposed_nodes}"
        sys_prompt += f"\nFeedback from those actions:\n" + "\n".join(feedbacks)

    messages_to_model = [SystemMessage(content=sys_prompt)] + messages

    decision = manager_agent.invoke(messages_to_model)

    if messages:
        add_conversation(messages, metadata={"routed_to": decision.next_node})

    return {
        "next_node": decision.next_node,
        "reasoning": decision.reasoning,
        "next_purpose": decision.next_purpose,
        "final_reply": decision.final_reply,
    }
