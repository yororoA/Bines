"""Compatibility wrapper for legacy callers; model actions use local Skills."""
from tools.dependencies import deps


def call_thinking_model(task_description: str, context: str = "") -> str:
    """Run a text-protocol skill task using the single configured model."""
    runner = deps.skill_runner
    helper = deps.thinking_model_helper
    if runner is None or helper is None:
        return "技能运行器尚未就绪。"
    messages = [
        {"role": "system", "content": "根据任务调用适用的本地技能。只执行用户明确授权的操作。"},
        {"role": "user", "content": f"任务：{task_description}\n上下文：{context or '无'}"},
    ]
    try:
        result, _, _ = helper.run_skill_protocol(messages, runner, max_iterations=10, temperature=0.1)
        return result
    except Exception as exc:
        return f"技能任务执行失败：{exc}"
