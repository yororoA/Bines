import zmq
import json
import threading
import sys
import os
import traceback
from pathlib import Path

# 确保可以从项目根目录导入 config
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import ZMQ_HOST, ZMQ_PORTS

# --- 关键修复 ---
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')
# ---------------

def exception_hook(type, value, tb):
    print("!!! Uncaught Exception !!!", flush=True)
    traceback.print_exception(type, value, tb)
sys.excepthook = exception_hook

# ZMQ Context
context = zmq.Context()

# SUB: 接收 ASR
sub_asr = context.socket(zmq.SUB)
sub_asr.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['HEARING_ASR_PUB']}")
sub_asr.setsockopt_string(zmq.SUBSCRIBE, "asr")

# SUB: 接收 Manual Text Input
sub_text = context.socket(zmq.SUB)
sub_text.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['MANUAL_TEXT_PUB']}")
sub_text.setsockopt_string(zmq.SUBSCRIBE, "") # Topic: "text" -> Receive all and filter later

# SUB: 接收 Visual
sub_vis = context.socket(zmq.SUB)
sub_vis.connect(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['VISUAL_PUB']}")
sub_vis.setsockopt_string(zmq.SUBSCRIBE, "visual")

# SUB: 接收 QQ 消息
QQ_PUB_PORT = ZMQ_PORTS.get('QQ_PUB', 5588)
sub_qq = context.socket(zmq.SUB)
try:
    sub_qq.connect(f"tcp://{ZMQ_HOST}:{QQ_PUB_PORT}")
    sub_qq.setsockopt_string(zmq.SUBSCRIBE, "qq")
except Exception as e:
    print(f"Warning: Failed to connect to QQ PUB: {e}")

# PUB: 发送处理后的结果给 Thinking
pub_res = context.socket(zmq.PUB)
pub_res.bind(f"tcp://*:{ZMQ_PORTS['CLASSIFICATION_PUB']}")
zmq_lock = threading.Lock()

# 除 Thinking 外需上报就绪的模块（与 start_modules.py 中名称一致）
EXPECTED_MODULES = {"Display", "Speaking", "Visual", "RAG Server", "Hearing"}
latest_visual_info = {"faces": [], "objects": [], "gestures": []}


def module_ready_collector():
    """接收各模块就绪上报，收齐后通知 Thinking 正式启动（上线、摘要/日记等）。"""
    rep = context.socket(zmq.REP)
    rep.bind(f"tcp://*:{ZMQ_PORTS['MODULE_READY_REP']}")
    ready = set()
    try:
        while len(ready) < len(EXPECTED_MODULES):
            try:
                msg = rep.recv_json()
            except Exception as e:
                print(f"[Classification] 接收就绪消息异常: {e}", flush=True)
                try:
                    rep.send_json({"ok": False, "error": str(e)})
                except Exception:
                    pass
                continue
            name = (msg or {}).get("module") or (msg or {}).get("name")
            if name and name in EXPECTED_MODULES:
                ready.add(name)
                print(f"[Classification] 收到就绪: {name} ({len(ready)}/{len(EXPECTED_MODULES)})", flush=True)
            try:
                rep.send_json({"ok": True})
            except Exception as e:
                print(f"[Classification] 回复就绪消息异常: {e}", flush=True)
                break
        # 收齐后通知 Thinking
        start_port = ZMQ_PORTS.get("START_THINKING_REP")
        if start_port:
            req = context.socket(zmq.REQ)
            req.setsockopt(zmq.LINGER, 2000)
            req.setsockopt(zmq.RCVTIMEO, 10000)
            req.setsockopt(zmq.SNDTIMEO, 2000)
            try:
                req.connect(f"tcp://127.0.0.1:{start_port}")
                req.send_string("start")
                req.recv_string()
                print("[Classification] 已通知 Thinking 正式启动（上线、摘要/日记等）", flush=True)
            except Exception as e:
                print(f"[Classification] 通知 Thinking 失败: {e}", flush=True)
            finally:
                try:
                    req.close()
                except Exception:
                    pass
        else:
            print("[Classification] START_THINKING_REP 未配置，未通知 Thinking", flush=True)
    except Exception as e:
        print(f"[Classification] module_ready_collector 异常: {e}", flush=True)
        traceback.print_exc()
    finally:
        try:
            rep.close()
        except Exception:
            pass

def visual_listener():
    global latest_visual_info
    print("Visual Listener started...", flush=True)
    while True:
        try:
            topic, msg = sub_vis.recv_multipart()
            data = json.loads(msg.decode('utf-8'))
            latest_visual_info = data
        except Exception as e:
            print(f"Visual receive error: {e}", flush=True)

def process_and_forward(user_input, source="ASR", extra_data=None):
    global latest_visual_info
    if not user_input: return
    
    print(f"\n[CLS] Received from {source}: {user_input}", flush=True)
    
    vis_desc_str = ""
    # (Visual info merging logic)
    if latest_visual_info.get('faces'):
        vis_desc_str += f"面前的人是: {','.join(latest_visual_info['faces'])}。 "
    if latest_visual_info.get('objects'):
        vis_desc_str += f"环境中有: {','.join(latest_visual_info['objects'])}。 "
    if latest_visual_info.get('gestures'):
        vis_desc_str += f"用户手势: {','.join(latest_visual_info['gestures'])}。"
    if latest_visual_info.get('scene_caption'):
        caption = latest_visual_info['scene_caption'].replace('\n', ' ')
        vis_desc_str += f" [场景视觉描述: {caption}]"

    # [用户请求] 清除缓存，防止二次对话时使用过期视觉信息
    # 只有当视觉模块主动推送新信息时，这里才会有值
    if source != "QQ": # QQ 消息不消耗视觉缓存，因为视觉是针对摄像头的
        latest_visual_info = {"faces": [], "objects": [], "gestures": []}
        
    final_payload = {
        "user_input": user_input,
        "img_descr": vis_desc_str,
        "sender": "CLASSIFIER",
        "source": source
    }
    
    if extra_data:
        final_payload.update(extra_data)
    
    print(f"[CLS] Forwarding to Thinking...", flush=True)
    with zmq_lock:
        pub_res.send_multipart([b"classified", json.dumps(final_payload).encode('utf-8')])

def classification_loop():
    print("Classification Module Loop started (Poller Mode)...", flush=True)
    
    poller = zmq.Poller()
    poller.register(sub_asr, zmq.POLLIN)
    poller.register(sub_text, zmq.POLLIN)
    poller.register(sub_qq, zmq.POLLIN) # 注册 QQ socket
    
    while True:
        try:
            # 【修复】减少poll超时时间到10ms，提高响应速度
            socks = dict(poller.poll(10)) # 10ms timeout
            
            # 1. Handle ASR
            if sub_asr in socks and socks[sub_asr] == zmq.POLLIN:
                topic, msg = sub_asr.recv_multipart()
                data = json.loads(msg.decode('utf-8'))
                process_and_forward(data.get("user_input", ""), source="ASR")
            
            # 2. Handle Text
            if sub_text in socks and socks[sub_text] == zmq.POLLIN:
                topic, msg = sub_text.recv_multipart()
                try:
                    print(f"[CLS] Manual Input Received (Topic: {topic})", flush=True)
                except:
                    pass
                try:
                    data = json.loads(msg.decode('utf-8'))
                    process_and_forward(data.get("user_input", ""), source="MANUAL")
                except json.JSONDecodeError:
                    print(f"[CLS] Error decoding JSON from manual input", flush=True)
            
            # 3. Handle QQ
            if sub_qq in socks and socks[sub_qq] == zmq.POLLIN:
                try:
                    topic, msg = sub_qq.recv_multipart()
                    # qq msg: {"user_id":..., "message":..., ...}
                    data = json.loads(msg.decode('utf-8'))
                    
                    is_mentioned = data.get("_is_mentioned", True)
                    is_admin = data.get("_is_admin", False)
                    sender_name = data.get("sender", {}).get("nickname", "未知用户")
                    raw_msg = data.get("raw_message", "")
                    group_id = data.get("group_id")
                    user_id = data.get("user_id")
                    
                    # [新增] 提取图片 URL（由 chatBot/main.py 预处理）
                    image_urls = data.get("_image_urls", [])
                    
                    # [新增] 清理 raw_message 中的 CQ:image 码，替换为可读占位符
                    import re as _re
                    if image_urls:
                        # 将 [CQ:image,...] 替换为 [图片]
                        raw_msg = _re.sub(r'\[CQ:image[^\]]*\]', '[图片]', raw_msg).strip()
                    
                    # 如果消息只有图片没有文字，设置合理的文本
                    if not raw_msg or raw_msg == '[图片]' * len(image_urls):
                        if image_urls:
                            raw_msg = f"[发送了{len(image_urls)}张图片]"

                    # 如果是管理员，给名字打上标签
                    display_sender_name = sender_name
                    if is_admin:
                        display_sender_name = f"{sender_name} (管理员)"
                    
                    # 额外信息
                    extra = {
                        "qq_context": {
                            "user_id": user_id,
                            "group_id": group_id,
                            "sender_name": sender_name,
                            "is_admin": is_admin,
                            "image_urls": image_urls  # [新增] 传递图片 URL
                        }
                    }

                    if is_mentioned:
                        # 构造提示词前缀，让 Thinking 知道这是 QQ 消息
                        if group_id:
                            input_text = f"[QQ群消息][{display_sender_name}]: {raw_msg}"
                        else:
                            input_text = f"[QQ私聊][{display_sender_name}]: {raw_msg}"
                        
                        process_and_forward(input_text, source="QQ", extra_data=extra)
                    else:
                        # 未被艾特，发送到后台日志 Topic (存入 RAG)
                        # [新增] 清理日志中的 CQ 码
                        log_content = _re.sub(r'\[CQ:image[^\]]*\]', '[图片]', raw_msg).strip() if raw_msg else raw_msg
                        log_payload = {
                            "content": log_content or raw_msg,
                            "sender": display_sender_name,
                            "group_id": group_id,
                            "user_id": user_id,
                            "timestamp": data.get("time", __import__('time').time()),
                            "is_group": True,
                            "is_admin": is_admin,
                            "image_urls": image_urls  # [新增] 传递图片URL供RAG记录
                        }
                        with zmq_lock:
                            pub_res.send_multipart([b"qq_log", json.dumps(log_payload).encode('utf-8')])

                except Exception as e:
                    print(f"QQ Msg Error: {e}", flush=True)
                    traceback.print_exc()
        
        except Exception as e:
            print(f"Main Loop Error: {e}", flush=True)
            traceback.print_exc()

if __name__ == "__main__":
    t_collector = threading.Thread(target=module_ready_collector, daemon=False)
    t_collector.start()
    print("[Classification] 模块就绪收集线程已启动，等待 Display/Speaking/Visual/RAG Server/Hearing 就绪后通知 Thinking", flush=True)
    t_vis = threading.Thread(target=visual_listener, daemon=True)
    t_vis.start()
    classification_loop()