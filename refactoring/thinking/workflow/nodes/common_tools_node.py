from langchain.tools import tool
from datetime import datetime


@tool
def get_time() -> str:
    """
    获取当前时间

    Returns:
        str: 当前时间，格式为 "%Y-%m-%d %H:%M:%S"
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


CommonTools = [get_time]
CommonToolsByName = {tool.name: tool for tool in CommonTools}

# def CommonToolsNode(state)
