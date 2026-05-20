from utils import generate_langchain_model
from thinking_settings import thinking_settings

# from common_tools_node import CommonTools
from status import GraphStatus, ManagerRoute
from langgraph.types import Send, Command
from langgraph.graph import END


_model = generate_langchain_model(thinking_settings.MODEL_SELECTED)
# ManagerModel = _model.bind_tools(CommonTools).with_structured_output(ManagerRoute)
ManagerModel = _model.with_structured_output(ManagerRoute)


def ManagerNode(state: GraphStatus) -> Command:
    result: ManagerRoute = ManagerModel.invoke(state)

    # 获取已完成的 ID 集合
    done_ids = set()
    for items in state.get("tasks_done", {}).values():
        if not items:
            continue
        for item in items:
            task_id = getattr(item, "task_id", None) or (
                item.get("task_id") if isinstance(item, dict) else None
            )
            if task_id:
                done_ids.add(task_id)

    sends = []
    for task_name, task_list in result.tasks_demand.items():
        # 处理 final_reply 和 advance_reply 任务
        if task_name in ["final_reply", "advance_reply"]:
            isFinal = task_name == "final_reply"
            reply_command = Send(
                to="reply",
                content={
                    "tasks": task_list,
                    "Final": isFinal,
                    "message": (
                        result.final_reply if isFinal else result.advance_reply
                    ),
                },
            )
            # 如果是 final_reply 任务，直接跳过后续任务
            if isFinal:
                return Command(
                    update=result.model_dump(),
                    goto=[reply_command],
                )
            sends.append(reply_command)
            continue

        for item in task_list:
            task_id = getattr(item, "task_id", None) or (
                item.get("task_id") if isinstance(item, dict) else None
            )
            if task_id and task_id not in done_ids:
                sends.append(
                    Send(
                        to=task_name,
                        content=item,
                    )
                )

    return Command(
        # result 是 Pydantic 实例，model_dump 会将其及其嵌套的 TaskItem 全部转为 dict
        update=result.model_dump(),
        goto=sends if sends else END,
    )
