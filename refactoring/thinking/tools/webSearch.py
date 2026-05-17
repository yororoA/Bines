from smolagents import CodeAgent, DuckDuckGoSearchTool
from utils import smolagents_model
from langchain.tools import tool
from thinking_settings import thinking_settings

_SEARCH_AGENT = None


@tool()
def webSearch(query: str) -> str:
    """Search the web for information based on a query using DuckDuckGo."""
    global _SEARCH_AGENT
    if _SEARCH_AGENT is None:
        _SEARCH_AGENT = CodeAgent(
            name="WebSearchAgent",
            description="an agent for web searching using DuckDuckGoSearchTool",
            model=smolagents_model(model=thinking_settings.MODEL_SELECTED),
            tools=[DuckDuckGoSearchTool()],
            additional_authorized_imports=["datetime", "requests", "httpx", "json"],
            system_prompt="You are a helpful assistant for searching the web using DuckDuckGoSearchTool. Use the tool to search the web and provide concise and relevant information based on the query. Make sure you know the current time before searching.",
            # executor_type="e2b",
            max_retries=3,
            verbosity_level=0,
            max_steps=5,
        )
    return str(_SEARCH_AGENT.run(query))
