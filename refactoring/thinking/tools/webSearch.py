from smolagents import (
    DuckDuckGoSearchTool,
    WebSearchTool,
    VisitWebpageTool,
    OpenAIModel,
    CodeAgent,
)
from langchain.tools import tool
from thinking_settings import thinking_settings

_model = OpenAIModel(
    model_id=thinking_settings.MODEL_LIST[2],  # mimo-v2.5
    api_base=thinking_settings.MIMO_API_URL,
    api_key=thinking_settings.MIMO_API_KEY,
)

_searchAgent = Null


@tool
def web_search(query: str) -> str:
    """Search the web for the answer to a question.

    Args:
        query: The question to search the web for.

    Returns:
        The answer to the question.

    Example:
        web_search("Who is the current president of the United States?") -> "The current president is Joe Biden."

    """
    global _searchAgent
    if _searchAgent is None:
        _searchAgent = CodeAgent(
            model=_model,
            tools=[DuckDuckGoSearchTool(), WebSearchTool(), VisitWebpageTool()],
            additional_authorized_imports=["datetime", "requests", "json", "httpx"],
            system_prompt="You are a helpful assistant that can search the web. Always make sure you know the current time.",
            max_tokens=1024,
            max_retries=3,
            # output_type="text",
        )

    return str(_searchAgent.run(query))
