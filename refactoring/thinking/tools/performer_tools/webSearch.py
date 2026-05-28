from smolagents import tool, DuckDuckGoSearchTool, WebSearchTool, VisitWebpageTool


@tool
def webSearch(query: str) -> str:
    """Search the web for the answer to a question.

    Args:
        query: The question to search the web for.

    Returns:
        The answer to the question.

    Example:
        webSearch("Who is the current president of the United States?") -> "The current president is Joe Biden."
    """
    search_tool = DuckDuckGoSearchTool()
    return str(search_tool(query))


def get_search_tools() -> list:
    return [DuckDuckGoSearchTool(), WebSearchTool(), VisitWebpageTool()]


SEARCH_AUTHORIZED_IMPORTS = ["datetime", "requests", "json", "httpx"]
