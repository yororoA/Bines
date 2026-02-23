import sys
import os
import json
import numpy as np
import cv2
import mediapipe as mp


# 1. 原始识别类
class HandInteractionSystem:
    def __init__(self):
        try:
            self.mp_hands = mp.solutions.hands
            self.hands = self.mp_hands.Hands(
                static_image_mode=False,  # 必须为 False 才能利用视频流的时间相关性（防抖的基础）
                max_num_hands=2,
                model_complexity=1,  # 0=快, 1=准 (建议改为1)
                min_detection_confidence=0.5,  # 恢复到 0.5，避免误识别背景为手
                min_tracking_confidence=0.4    # 保持较低的追踪门槛，一旦识别到手，尽量不丢失
            )
        except AttributeError:
            sys.stderr.write("Critical Error: mp.solutions.hands load failed.\n")
            raise

    def process_hands(self, frame):
        # 假设输入是 BGR (OpenCV标准)，转 RGB
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.hands.process(img_rgb)
        return results


# 2. 主循环
if sys.platform == "win32":
    import msvcrt

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def main():
    try:
        hand_system = HandInteractionSystem()
    except Exception as e:
        sys.stderr.write(f"Init Error: {e}\n")
        return

    while True:
        try:
            # 读取头
            header = sys.stdin.buffer.read(8)
            if not header or len(header) < 8:
                break
            width = int.from_bytes(header[0:4], byteorder='big')
            height = int.from_bytes(header[4:8], byteorder='big')

            # 读取图
            frame_size = width * height * 3
            frame_data = sys.stdin.buffer.read(frame_size)
            if len(frame_data) < frame_size:
                break

            frame = np.frombuffer(frame_data, dtype=np.uint8).reshape((height, width, 3))

            # 识别
            results = hand_system.process_hands(frame)

            # --- 改进：返回所有关键点 (包含 World Landmarks) ---
            hands_list = []
            if results.multi_hand_landmarks:
                # 获取 World Landmarks (真实物理坐标，单位：米)
                world_landmarks_list = results.multi_hand_world_landmarks or [None] * len(results.multi_hand_landmarks)

                for hand_landmarks, world_landmarks in zip(results.multi_hand_landmarks, world_landmarks_list):
                    # 提取所有 21 个点
                    landmarks = {}
                    key_indices = [i for i in range(21)]

                    for i in key_indices:
                        lm = hand_landmarks.landmark[i]
                        point_data = {"x": lm.x, "y": lm.y, "z": lm.z}
                        
                        # 如果有真实世界坐标，也一并发送
                        if world_landmarks:
                            wlm = world_landmarks.landmark[i]
                            point_data["wx"] = wlm.x
                            point_data["wy"] = wlm.y
                            point_data["wz"] = wlm.z
                            
                        landmarks[str(i)] = point_data

                    hands_list.append(landmarks)

            sys.stdout.write(json.dumps(hands_list) + "\n")
            sys.stdout.flush()

        except Exception:
            sys.stdout.write("[]\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()