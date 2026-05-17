from utils.chroma_store import add_conversation
from ..messagesState import MessagesState


def reply_node(state: MessagesState):
    final_reply = state["final_reply"] or "No final reply provided."
    print(f"Final reply to user: {final_reply}")

    messages = state.get("messages", [])
    if messages:
        add_conversation(messages, metadata={"node": "reply", "final_reply": final_reply})

    return {
        "final_reply": final_reply,
    }
