"""
视觉信息获取工具
用于获取当前看到的视觉信息，包括人脸、物体和场景描述
"""
import json
import zmq
from .dependencies import deps
from config import ZMQ_HOST, ZMQ_PORTS
from common.zmq_rpc import zmq_req_json

# 全局变量：当前视觉信息（用于缓存）
CURRENT_VISUAL_INFO = "暂无视觉信息"

def get_visual_info(focus=None):
    """
    获取当前看到的视觉信息，包括人脸、物体和场景描述。
    注意：这是一个主动操作，会触发摄像头拍摄并分析，可能需要几秒钟。
    
    Args:
        focus: 可选，指定要重点关注的问题或细节（例如："Does the user wear glasses?"）
               如果提供，视觉模块会针对这个问题进行详细分析。
    
    Returns:
        str: JSON格式的视觉信息字符串
    """
    # [依赖注入] 从依赖容器获取 ZMQ 上下文
    if deps.zmq_context is None:
        return "ZMQ context is not available."
    
    try:
        # 发送指令，包含 focus 参数（如果有）
        request_data = {"command": "look"}
        if focus:
            request_data["focus"] = focus

        response = zmq_req_json(
            deps.zmq_context,
            f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['VISUAL_REQREP']}",
            request_data,
            recv_timeout_ms=30000,
            send_timeout_ms=5000,
            linger_ms=1000,
        )

        # 简化返回给 LLM 的信息，避免 base64 图片太大
        if "scene_image" in response:
            del response["scene_image"]
        result = json.dumps(response, ensure_ascii=False)

        # 更新全局视觉信息（用于缓存）
        global CURRENT_VISUAL_INFO
        CURRENT_VISUAL_INFO = result

        return result
    except Exception as e:
        error_msg = f"Error getting visual info: {e}"
        print(f"[get_visual_info] {error_msg}", flush=True)
        return error_msg
