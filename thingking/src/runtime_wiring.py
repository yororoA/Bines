import time
import zmq


def register_tool_dependencies(deps, context, thinking_model_helper, get_tools_schema, get_tools_registry):
    """
    统一注册工具运行时依赖，避免入口文件散落装配代码。
    """
    deps.register_zmq_context(context)
    deps.register_thinking_model_helper(thinking_model_helper)
    deps.register_tools_accessors(
        get_tools_schema=get_tools_schema,
        get_tools_registry=get_tools_registry,
    )


def bind_pub_socket_with_retry(context: zmq.Context, port: int, label: str,
                               max_retries: int = 3, retry_delay: float = 1.0):
    """
    统一创建并绑定 PUB socket，内置重试机制。
    """
    sock = context.socket(zmq.PUB)
    endpoint = f"tcp://*:{port}"
    for retry in range(max_retries):
        try:
            sock.bind(endpoint)
            return sock
        except zmq.ZMQError:
            if retry < max_retries - 1:
                print(f"[Thinking] 端口 {port} ({label}) 绑定失败，等待 {retry_delay} 秒后重试...", flush=True)
                time.sleep(retry_delay)
            else:
                sock.close()
                raise
