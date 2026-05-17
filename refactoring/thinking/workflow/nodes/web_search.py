from langchain.messages import ToolMessage
from tools import webSearch
from ..messagesState import MessagesState
import uuid


def web_search_node(state: MessagesState):
    query = state["next_purpose"]
    search_result = str(webSearch(query))
    return {
        "messages": [
            ToolMessage(
                content=search_result, name="websearch", tool_call_id=str(uuid.uuid4())
            )
        ],
        "thinking": [
            f"Performed web search with query: {query} and got result: {search_result}"
        ],
        "purposed_nodes": ["web_search"],
        "purposed_feedbacks": [
            f"Web search for query '{query}' returned: {search_result}"
        ],
    }
