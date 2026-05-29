from smolagents import tool, DuckDuckGoSearchTool, WebSearchTool, VisitWebpageTool

_search_tool = None
_search_tools = None


def _get_search_tool():
    global _search_tool
    if _search_tool is None:
        _search_tool = DuckDuckGoSearchTool()
    return _search_tool


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
    return str(_get_search_tool()(query))


def get_search_tools() -> list:
    global _search_tools
    if _search_tools is None:
        _search_tools = [DuckDuckGoSearchTool(), WebSearchTool(), VisitWebpageTool()]
    return list(_search_tools)


SEARCH_AUTHORIZED_IMPORTS = ["datetime", "requests", "json", "httpx"]
