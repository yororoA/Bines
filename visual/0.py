import cv2
import threading
import sys
import traceback
import requests
import base64
import time
import json
import os
from pathlib import Path

# --- 防闪退补丁 ---
def exception_hook(type, value, tb):
    print("!!! Uncaught Exception !!!")
    traceback.print_exception(type, value, tb)
    input("Press Enter to exit...")
sys.excepthook = exception_hook
# -----------------

# 确保可以从项目根目录导入 config
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from models.face_models.face_recognise import face_identify
from models.face_models.face_recognise import face_input
from models.face_models.face_recognise import face_find
from models.face_models.face_recognise import load_embeddings, save_embeddings
from config import ZMQ_PORTS
from vlm_client import call_dashscope_vlm as _call_dashscope_vlm
# from models.object_models.object_recognise_onnx import ObjectDetector
# from models.gesture_models.gesture import HandInteractionSystem

# --- 1. 初始人脸录入 ---
FACE_DB_PATH = "models/face_models/face_db.pkl"

if not load_embeddings(FACE_DB_PATH):
    print("正在初始化人脸库...")
    try:
        owner = cv2.imread("models/face_models/saved_faces/owner.jpg")
        face_input(owner, "Yororo Ice")
        face_input(cv2.imread("models/face_models/saved_faces/owner2.jpg"), "Yororo Ice")
        face_input(cv2.imread("models/face_models/saved_faces/owner3.jpg"), "Yororo Ice")
        face_input(cv2.imread("models/face_models/saved_faces/owner4.jpg"), "Yororo Ice")
        face_input(cv2.imread("models/face_models/saved_faces/owner5.jpg"), "Yororo Ice")
        face_input(cv2.imread("models/face_models/saved_faces/owner6.jpg"), "Yororo Ice")
        face_input(cv2.imread("models/face_models/saved_faces/owner7.jpg"), "Yororo Ice")
        face_input(cv2.imread("models/face_models/saved_faces/owner8.jpg"), "Yororo Ice")
        face_input(cv2.imread("models/face_models/saved_faces/owner9.jpg"), "Yororo Ice")

        bajiao = cv2.imread("models/face_models/saved_faces/bajiao.jpg")
        face_input(bajiao, "Ba Jiao")

        # 录入完成后保存
        save_embeddings(FACE_DB_PATH)

    except Exception as e:
        print(f"录入过程发生错误: {e}")

# 物体识别
# detector = ObjectDetector(model_path="models/object_models/yolov8m.onnx", conf_thres=0.25)
# 降低阈值以提高对被遮挡物体的召回率
# detector = ObjectDetector(
#     model_path="models/object_models/v8_world/yolov8l-custom-baked2.onnx",
#     names_path="./models/object_models/v8_world/model_names2.txt",
#     conf_thres=0.15
# )


# 静态图片模块
def static_image_model():
    # 人脸测试
    def face_test():
        test = cv2.imread("./models/face_models/saved_faces/test/multiple_test.jpg")
        cv2.imshow("face test", face_find(test, find=False))
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    # 物体测试
    def object_test():
        pass
        # test = cv2.imread("./models/object_models/object_test/test1.png")
        # obj_res = detector.detect(test)
        # for (class_id, conf, box) in obj_res:
        #     x, y, w, h = box
        #     label_name = detector.classes[class_id]
        #     if label_name == "person":
        #         continue
        #     cv2.rectangle(test, (x, y), (x + w, y + h), (0, 165, 255), 2)
        #     cv2.putText(test, f"{label_name} {conf:.2f}", (x, y - 5),
        #                 cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 2)
        # cv2.imshow("object test", test)
        # cv2.waitKey(0)
        # cv2.destroyAllWindows()

    return {"face_test": face_test, "object_test": object_test}


# static_image_model()["object_test"]()


# 摄像头模块
def camera_model():
    # 初始化手势系统
    # hand_system = HandInteractionSystem()

    # --- ZMQ 初始化 (视觉发布) ---
    import zmq
    import json
    import time
    import base64
    context = zmq.Context()
    # PUB: 发送视觉感知信息
    vis_socket = context.socket(zmq.PUB)
    vis_socket.bind(f"tcp://*:{ZMQ_PORTS['VISUAL_PUB']}")
    
    # REP: 响应 Thinking 的主动调用
    rep_socket = context.socket(zmq.REP)
    rep_socket.bind(f"tcp://*:{ZMQ_PORTS['VISUAL_REQREP']}")

    last_send_time = 0

    # --- 2. 摄像头设置 ---
    cap = cv2.VideoCapture(0)
    cap.set(3, 640)
    cap.set(4, 480)

    if not cap.isOpened():
        print("Error: Could not open camera.")
        exit()
        
    # --- 3. 多线程变量 ---
    # 用于在主线程(显示)和子线程(计算)之间共享数据
    class SharedState:
        def __init__(self):
            self.frame_to_process = None  # 待处理的帧（旧流程，保留兼容）
            self.latest_frame = None  # 最新的摄像头帧（用于实时请求）
            self.results = ([], [], [], [], "")  # 最新的识别结果: 人脸, 物体, 手部原始数据, 所有物体, 场景描述
            self.running = True  # 线程运行标志
            self.lock = threading.Lock()  # 线程锁

    state = SharedState()
    
    # --- DashScope VLM 调用 ---
    def call_dashscope_vlm(image_b64, focus_question=None):
        # 根据是否有 focus_question 生成提示词
        # 新规则：
        # - 如果上游调用方提供了 focus_question，则直接视为完整提示词，不再在此处固定模板；
        # - 否则使用默认的通用描述提示。
        if focus_question:
            prompt_text = str(focus_question).strip()
        else:
            prompt_text = "简要描述画面中的内容（50字以内），可适当关注人物的情绪和动作。"
        result = _call_dashscope_vlm(
            image_b64,
            prompt_text,
            api_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            api_key=os.environ.get("DASHSCOPE_API_KEY"),
            model="qwen3-vl-flash",
            timeout=10,
            proxies={"http": None, "https": None},
            missing_key_message="",
            empty_message="",
            error_message_prefix="",
        )
        if result.startswith("[VLM 请求失败"):
            print(f"DashScope error: {result}")
            return ""
        return result or ""

    # --- 4. 定义后台识别/服务线程 ---
    def recognition_loop():
        print("后台识别服务已启动 (等待指令模式)")
        nonlocal last_send_time
        
        # 创建 CLAHE 对象 (用于光照增强)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        
        current_caption = ""
        previous_caption = ""  # 存储上一次的场景描述
        caption_similarity_threshold = 0.7  # 相似度阈值，超过此值认为内容重复

        # 计算两个文本的相似度（使用简单的词袋模型）
        def calculate_similarity(text1, text2):
            if not text1 or not text2:
                return 0.0
            words1 = set(text1.lower().split())
            words2 = set(text2.lower().split())
            intersection = len(words1 & words2)
            union = len(words1 | words2)
            return intersection / union if union > 0 else 0.0

        # 定义 update_caption 函数放在循环外
        def update_caption(b64, focus_question=None):
            # 注意: 在同步调用模式下，VLM 应该在返回前就把结果拿回来，
            # 而不是像之前那样异步开个线程去跑，否则 REQ 立即返回了但没有 caption。
            # 这里我们改为同步调用。
            desc = call_dashscope_vlm(b64, focus_question)
            return desc if desc else ""

        while state.running:
            # === 改动点: 阻塞等待 ZMQ 请求 ===
            try:
                # 接收指令，支持 JSON 格式（包含 focus 参数）或字符串格式（兼容旧版本）
                # REP socket 是阻塞式的，会一直等待直到收到消息
                # 先接收原始消息
                msg_bytes = rep_socket.recv()
                
                # 尝试解析为 JSON
                try:
                    request_data = json.loads(msg_bytes.decode('utf-8'))
                    message = request_data.get("command", "look")
                    focus_question = request_data.get("focus", None)
                except (json.JSONDecodeError, TypeError, UnicodeDecodeError, AttributeError):
                    # 如果不是 JSON，尝试作为字符串解析（兼容旧版本）
                    try:
                        message = msg_bytes.decode('utf-8')
                        focus_question = None
                    except:
                        # 如果都失败了，使用默认值
                        message = "look"
                        focus_question = None
                # print(f"收到视觉请求: {message}, focus: {focus_question}")
            except zmq.ZMQError as e:
                print(f"ZMQ接收错误: {e}")
                time.sleep(0.1)
                continue

            frame = None
            # === 修复: 获取主循环维护的最新帧，而不是使用可能过时的缓存帧 ===
            # 使用锁保护，确保线程安全
            with state.lock:
                if state.latest_frame is not None:
                    frame = state.latest_frame.copy()  # 复制帧，避免主循环修改影响
            
            if frame is None:
                rep_socket.send_json({"status": "error", "message": "Camera not ready"})
                continue

            # 开始处理
            h, w, _ = frame.shape
            
            # === 优化1: 减少图像转换次数 ===
            # 仅在需要时进行光照预处理
            if h * w > 640 * 480:  # 只有当图像分辨率较大时才进行光照增强
                # 光照预处理
                lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                l2 = clahe.apply(l)
                lab = cv2.merge((l2, a, b))
                frame_enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            else:
                # 小分辨率图像直接使用原图，减少处理时间
                frame_enhanced = frame
            
            # === 1. 人脸识别 ===
            face_results = face_identify(frame_enhanced)
            
            # === 2. 准备图像给 VLM ===
            # 优化2: 仅在有人脸识别结果时才绘制人脸框，减少不必要的绘制
            vlm_frame = frame.copy()
            if face_results:
                for (bbox, name) in face_results:
                    startX, startY, endX, endY = bbox
                    # 简化绘制，使用更高效的绘制函数
                    cv2.rectangle(vlm_frame, (startX, startY), (endX, endY), (0, 255, 0), 2)
                    y = startY - 10 if startY - 10 > 10 else startY + 10
                    # 优化3: 使用更高效的字体和绘制方式
                    cv2.putText(vlm_frame, name, (startX, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            # === 优化4: 调整JPEG编码质量，平衡质量和速度 ===
            # 降低JPEG质量到40，提高编码速度
            encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 40]
            _, buffer = cv2.imencode('.jpg', vlm_frame, encode_params)
            img_b64 = base64.b64encode(buffer).decode('utf-8')

            # === 3. VLM 调用 (同步) ===
            # 因为是应答模式，必须等结果
            # 传递 focus_question 参数（如果存在）
            current_caption = update_caption(img_b64, focus_question)

            # 检查内容是否与之前重复
            is_repeated = False
            if previous_caption and current_caption:
                similarity = calculate_similarity(current_caption, previous_caption)
                if similarity > caption_similarity_threshold:
                    is_repeated = True
                    print(f"⚠️ 检测到重复内容，相似度: {similarity:.2f}，跳过发送重复描述")

            # 构造结果
            vis_info = {
                "faces": [f[1] for f in face_results],
                "objects": [],
                "gestures": [],
                "scene_caption": current_caption if not is_repeated else "",
                "scene_image": img_b64 if not is_repeated else "",
                "status": "success",
                "is_repeated": is_repeated
            }

            # === 4. 发送回复 (REP) ===
            rep_socket.send_json(vis_info)

            # === 5. 同时广播给 Server (PUB) 以便系统其它部分感知 ===
            try:
                # 只广播非重复内容
                if not is_repeated:
                    vis_socket.send_multipart([b"visual", json.dumps(vis_info).encode('utf-8')])
            except:
                pass

            # 更新 UI 显示状态
            with state.lock:
                 state.results = (face_results, [], [], [], current_caption)

            # 更新之前的描述
            if current_caption:
                previous_caption = current_caption


    # 启动线程
    thread = threading.Thread(target=recognition_loop)
    thread.daemon = True  # 设置为守护线程，主程序退出它也退出
    thread.start()

    try:
        from common.module_ready import notify_module_ready
        notify_module_ready("Visual")
    except Exception as e:
        print(f"[Visual] 就绪上报失败: {e}", flush=True)

    print("开始视频流... 按 Ctrl+C 退出（窗口显示已禁用）")

    # --- 辅助函数：绘制中文文本 ---
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np

    def cv2_draw_text(img, text, pos, color=(0, 255, 0), size=20):
        try:
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(pil_img)
            # 尝试加载中文字体，如果失败则使用默认
            font = None
            try:
                # Windows 常见中文字体
                font = ImageFont.truetype("msyh.ttf", size)
            except:
                try:
                    font = ImageFont.truetype("simhei.ttf", size)
                except:
                    # Linux/Mac fallback or default
                    font = ImageFont.load_default()
            
            draw.text(pos, text, font=font, fill=color[::-1]) # RGB -> BGR for PIL fill? No, PIL uses RGB. 
            # cv2 color is BGR (0, 255, 0) -> Green. 
            # PIL fill=(0, 255, 0) -> Green.
            # But we are passing tuple. Let's assume input color is BGR (since it's cv2 context).
            # So we need to flip it to RGB for PIL. 
            
            img_result = cv2.cvtColor(np.asarray(pil_img), cv2.COLOR_RGB2BGR)
            return img_result
        except Exception as e:
            # print(f"Draw text error: {e}")
            return img

    def cleanup():
        """清理所有资源"""
        print("🔄 正在清理视觉模块资源...")
        
        # 1. 释放ZMQ资源
        try:
            rep_socket.close()
            vis_socket.close()
            context.term()
            print("✅ ZMQ资源已释放")
        except Exception as e:
            print(f"❌ 释放ZMQ资源失败: {e}")
        
        # 2. 释放摄像头资源
        try:
            cap.release()
            print("✅ 摄像头资源已释放")
        except Exception as e:
            print(f"❌ 释放摄像头资源失败: {e}")
        
        # 3. 关闭所有OpenCV窗口
        try:
            cv2.destroyAllWindows()
            print("✅ OpenCV窗口已关闭")
        except Exception as e:
            print(f"❌ 关闭OpenCV窗口失败: {e}")
        
        print("✅ 视觉模块资源清理完成")
    
    # --- 5. 主循环  ---
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)  # 水平反转
            h, w = frame.shape[:2]

            # 1. 更新最新帧（用于实时请求）
            with state.lock:
                state.latest_frame = frame.copy()  # 保存最新帧的副本
                # 把当前帧塞给后台线程（旧流程，保留兼容）
                if state.frame_to_process is None:
                    state.frame_to_process = frame

            # 2. 获取当前的识别结果
            face_res, obj_res, hand_res, all_objs, caption = [], [], [], [], ""
            with state.lock:
                if len(state.results) == 5:
                    face_res, obj_res, hand_res, all_objs, caption = state.results
                elif len(state.results) == 4:
                    face_res, obj_res, hand_res, all_objs = state.results
                    caption = ""
                else:
                    # 兼容旧格式
                    face_res, obj_res, hand_res = state.results[:3]

            # 3. 绘制结果
            # 显示 VLM 字幕 (中文支持)
            if caption:
                 # 简单的自动换行/截断显示 (只显示前 50 字防止遮挡太多)
                 lines = [caption[i:i+35] for i in range(0, len(caption), 35)]
                 for i, line in enumerate(lines[:3]): # 最多显示3行
                     frame = cv2_draw_text(frame, line, (10, h - 80 + i*25), (0, 255, 255), size=20)

            # 人脸
            for result in face_res:
                bbox = result[0]
                name = result[1]
                startX, startY, endX, endY = bbox
                color = (0, 255, 0) if name != "Unknown" else (0, 0, 255)
                y = startY - 10 if startY - 10 > 10 else startY + 10
                
                cv2.rectangle(frame, (startX, startY), (endX, endY), color, 2)
                # 使用中文绘制名字
                frame = cv2_draw_text(frame, name, (startX, y - 20), color, size=20)

            # 绘制背景物体 (未被交互的) - 用灰色显示，方便用户瞄准
            # 跳过绘制，减少处理时间
            # for (class_id, conf, box) in all_objs:
            #      x, y, w, h = box
            #      # 简单的灰色框
            #      cv2.rectangle(frame, (x, y), (x + w, y + h), (100, 100, 100), 1)
            #      # 可选：显示类别名
            #      if 0 <= class_id < len(detector.classes):
            #          name = detector.classes[class_id]
            #          cv2.putText(frame, name, (x, y-5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 100, 100), 1)

            # 绘制手部骨架
            if hand_res:
                h, w, _ = frame.shape
                for hand in hand_res:
                    # 获取手势状态
                    gesure_state = hand.get('state', 'UNKNOWN')
                    # 获取手腕坐标用于显示文字
                    wrist_x, wrist_y = 0, 0
                    if '0' in hand:
                        wrist_x = int(hand['0']['x'] * w)
                        wrist_y = int(hand['0']['y'] * h)
                    # 根据状态设置颜色和文字
                    # 握拳=红色，捏合=黄色，张开=绿色
                    color = (0, 255, 0)
                    if gesure_state == "FIST":
                        color = (0, 0, 255)
                    elif gesure_state == "PINCH":
                        color = (0, 255, 255)
                    # 画出状态文字
                    cv2.putText(frame, gesure_state, (wrist_x, wrist_y - 20),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
                    # 画骨架 (简化版)
                    if '8' in hand:  # 食指尖
                        tx, ty = int(hand['8']['x'] * w), int(hand['8']['y'] * h)
                        cv2.circle(frame, (tx, ty), 5, color, -1)
                        if wrist_x: cv2.line(frame, (wrist_x, wrist_y), (tx, ty), color, 2)
                    # 如果是捏合，额外画一条线连接拇指和食指
                    if gesure_state == "PINCH" and '4' in hand and '8' in hand:
                        fx, fy = int(hand['4']['x'] * w), int(hand['4']['y'] * h)
                        sx, sy = int(hand['8']['x'] * w), int(hand['8']['y'] * h)
                        cv2.line(frame, (fx, fy), (sx, sy), (255, 0, 255), 3)

            # 物体 (排除 person ), 此时只包含与手有交互的物体
            # 跳过绘制，减少处理时间
            # for (class_id, conf, box) in obj_res:
            #     x, y, w, h = box
            #     # 安全检查：防止 class_id 超出 classes 列表范围
            #     if 0 <= class_id < len(detector.classes):
            #         label_name = detector.classes[class_id]
            #     else:
            #         label_name = f"Unknown({class_id})"

            #     if label_name == "person":
            #         continue
            #     color = (255, 0, 255)  # 被选中物体
            #     cv2.rectangle(frame, (x, y), (x + w, y + h), color, 3)
            #     cv2.putText(frame, f"Target: {label_name}", (x, y - 10),
            #                 cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

            # 4. 【优化】不再创建窗口显示图像，减少资源占用
            # cv2.imshow('Camera', frame)  # 已禁用窗口显示
            
            # 检查退出信号（通过其他方式，如文件标志或信号）
            # 由于不再使用 cv2.waitKey，需要通过其他方式检查退出
            # 保持循环运行，直到收到退出信号
            time.sleep(0.01)  # 短暂休眠，避免CPU占用过高
    except KeyboardInterrupt:
        print("\n⏹️ 收到中断信号，正在退出...")
        state.running = False
    finally:
        cleanup()


camera_model()
