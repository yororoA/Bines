from langchain.messages import ToolMessage
from utils.chroma_store import retrieve_relevant_str
from ..messagesState import MessagesState
import uuid


def rag_search_node(state: MessagesState):
    query = state["next_purpose"]
    rag_result = retrieve_relevant_str(query)
    if not rag_result:
        rag_result = "No relevant context found in the knowledge base."
    return {
        "messages": [
            ToolMessage(
                content=rag_result, name="rag_search", tool_call_id=str(uuid.uuid4())
            )
        ],
        "thinking": [
            f"Performed RAG search with query: {query} and got result: {rag_result}"
        ],
        "purposed_nodes": ["rag_search"],
        "purposed_feedbacks": [
            f"RAG search for query '{query}' returned: {rag_result}"
        ],
    }
