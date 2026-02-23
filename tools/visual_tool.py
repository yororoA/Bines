"""
视觉信息获取工具
用于获取当前看到的视觉信息，包括人脸、物体和场景描述
"""
import json
import zmq
from .dependencies import deps
from config import ZMQ_HOST, ZMQ_PORTS

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
    
    # 临时创建一个 REQ socket 连接到 Visual 模块
    req_socket = None
    try:
        req_socket = deps.zmq_context.socket(zmq.REQ)
        req_socket.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['VISUAL_REQREP']}")
        
        # 发送指令，包含 focus 参数（如果有）
        request_data = {"command": "look"}
        if focus:
            request_data["focus"] = focus
        req_socket.send_json(request_data)

        # 使用 Poller 设置超时，避免死锁
        poller = zmq.Poller()
        poller.register(req_socket, zmq.POLLIN)
        
        # 等待回复，超时设定为 30秒 (VLM可能较慢)
        if poller.poll(30000):
            response = req_socket.recv_json()
            # 简化返回给 LLM 的信息，避免 base64 图片太大
            if "scene_image" in response:
                del response["scene_image"]
            result = json.dumps(response, ensure_ascii=False)
            
            # 更新全局视觉信息（用于缓存）
            global CURRENT_VISUAL_INFO
            CURRENT_VISUAL_INFO = result
            
            return result
        else:
            return "Visual module timed out or is not reachable."
    except Exception as e:
        error_msg = f"Error getting visual info: {e}"
        print(f"[get_visual_info] {error_msg}", flush=True)
        return error_msg
    finally:
        if req_socket:
            try:
                req_socket.close()
            except:
                pass
