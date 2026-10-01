def register_runtime_dependencies(deps, context, thinking_model_helper, skill_runner):
    """
    统一注册工具运行时依赖，避免入口文件散落装配代码。
    """
    deps.register_zmq_context(context)
    deps.register_thinking_model_helper(thinking_model_helper)
    deps.register_skill_runner(skill_runner)
