from utils import generate_langchain_model
from thinking_settings import thinking_settings

# from common_tools_node import CommonTools
from status import GraphStatus, ManagerRoute
from langgraph.types import Send, Command

_model = generate_langchain_model(thinking_settings.MODEL_SELECTED)
# ManagerModel = _model.bind_tools(CommonTools).with_structured_output(ManagerRoute)
ManagerModel = _model.with_structured_output(ManagerRoute)


def ManagerNode(state: GraphStatus) -> ManagerRoute:
    """The manager node."""
    result = ManagerModel.invoke(state)
    sends = []
    for task_name, task_descriptions in result.tasks_demand.items():
        for task_description in task_descriptions:
            sends.append(
                Send(
                    to=task_name,
                    content=task_description,
                )
            )

    return Command(update=result, goto=sends)
