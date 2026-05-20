from smolagents import (
    DuckDuckGoSearchTool,
    WebSearchTool,
    VisitWebpageTool,
    CodeAgent,
)
from langchain.tools import tool
from thinking_settings import thinking_settings
from utils import generate_sml_model


_model = generate_sml_model(thinking_settings.MODEL_SELECTED)

_searchAgent = None


@tool
def webSearch(query: str) -> str:
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
            max_steps=6,
        )

    return str(_searchAgent.run(query))
