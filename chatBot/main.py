import time
import zmq
import json
import traceback
import sys
import os
from pathlib import Path

# 确保可以从项目根目录导入 config
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from chatBot.napcat_client import NapCatClient
from chatBot.config import ONEBOT_WS_URL, ONEBOT_ACCESS_TOKEN, ZMQ_PUB_PORT, BOT_QQ_ID, ADMIN_QQ_LIST

def main():
    print(f"[ChatBot] Starting NapCatQQ Client...", flush=True)
    
    # 1. 初始化 ZMQ 发布者
    context = zmq.Context()
    pub_socket = context.socket(zmq.PUB)
    pub_socket.bind(f"tcp://*:{ZMQ_PUB_PORT}")
    print(f"[ChatBot] ZMQ PUB bound to port {ZMQ_PUB_PORT}", flush=True)

    # 2. 初始化 NapCat 客户端
    client = NapCatClient(ws_url=ONEBOT_WS_URL, token=ONEBOT_ACCESS_TOKEN)

    # 3. 注册消息监听
    @client.on("message")
    def handle_message(event):
        try:
            message_type = event.get("message_type")
            raw_message = event.get("raw_message", "")
            user_id = event.get("user_id")

            # 过滤掉自己发送的消息 (NapCat 默认会上报自身消息)
            if user_id == BOT_QQ_ID:
                return

            # 群聊消息处理
            is_mentioned = True # 默认为True（对于私聊等）
            if message_type == "group":
                is_mentioned = False
                # 检查 message 数组中的 at 消息段
                message_chain = event.get("message", [])
                if isinstance(message_chain, list):
                    for segment in message_chain:
                        if segment.get("type") == "at":
                            qq_str = str(segment.get("data", {}).get("qq", ""))
                            if qq_str == str(BOT_QQ_ID):
                                is_mentioned = True
                                break
                # 兼容 raw_message 检查 (防御性)
                if not is_mentioned and f"[CQ:at,qq={BOT_QQ_ID}]" in raw_message:
                     is_mentioned = True
            
            # 标记是否被艾特，供后续模块区分处理
            event["_is_mentioned"] = is_mentioned

            # 标记是否为管理员消息
            if user_id in ADMIN_QQ_LIST:
                event["_is_admin"] = True
            else:
                event["_is_admin"] = False

            # [新增] 从 message chain 提取图片 URL
            image_urls = []
            message_chain = event.get("message", [])
            if isinstance(message_chain, list):
                for segment in message_chain:
                    if segment.get("type") == "image":
                        img_data = segment.get("data", {})
                        # NapCat/Go-CQHTTP 通常会提供 url 字段
                        img_url = img_data.get("url") or img_data.get("file")
                        if img_url:
                            image_urls.append(img_url)
            if image_urls:
                event["_image_urls"] = image_urls
                print(f"[ChatBot] Extracted {len(image_urls)} image(s) from message", flush=True)

            # print(f"[ChatBot] Received {message_type}: {raw_message[:20]}... (Mentioned: {is_mentioned})", flush=True)
            
            # 构造 ZMQ 消息
            # Topic: "qq"
            # Payload: 原始 event json
            pub_socket.send_multipart([b"qq", json.dumps(event).encode("utf-8")])
            
        except Exception as e:
            print(f"[ChatBot] Error handling message: {e}", flush=True)
            traceback.print_exc()

    @client.on("notice")
    def handle_notice(event):
        # 可以选择性转发通知，例如群成员变动
        pass

    @client.on("request")
    def handle_request(event):
        # 转发加好友/加群请求
        try:
            pub_socket.send_multipart([b"qq", json.dumps(event).encode("utf-8")])
        except Exception:
            pass

    # 4. 启动连接 (阻塞模式)
    print(f"[ChatBot] Connecting to {ONEBOT_WS_URL}...", flush=True)
    # 我们使用 run_forever=True (在 connect 内部实现为阻塞或非阻塞取决于参数)
    # 之前修改 NapCatClient.connect 时，默认 background=True
    # 这里我们需要主线程阻塞，所以...
    
    client.connect(background=True)
    
    try:
        while True:
            time.sleep(1)
            if not client.is_connected:
                print("[ChatBot] WebSocket disconnected, retrying in 5s...", flush=True)
                time.sleep(5)
                client.connect(background=True)
    except KeyboardInterrupt:
        print("[ChatBot] Stopping...", flush=True)
        client.disconnect()

if __name__ == "__main__":
    main()
