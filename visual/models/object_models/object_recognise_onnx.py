import os.path

import cv2
import numpy as np
import onnxruntime as ort


class ObjectDetector:
    def __init__(self, model_path="object_models/v8n/yolov8n.onnx", names_path=None, conf_thres=0.4, iou_thres=0.45):
        self.conf_threshold = conf_thres
        self.iou_threshold = iou_thres

        # 初始化 ONNX Runtime
        # 配置使用 GPU 0（第一张 NVIDIA 显卡）
        # 注意：CUDAExecutionProvider 只会看支持 CUDA 的设备（NVIDIA 显卡），会自动忽略 Intel 核显
        # 在 CUDA 的眼中，RTX 4060 是它看到的第一张卡，ID 是 0
        # device_id=0 表示使用第一张 NVIDIA 显卡（GPU 0）
        providers = [
            ('CUDAExecutionProvider', {'device_id': 0}),
            'CPUExecutionProvider'
        ]
        try:
            self.session = ort.InferenceSession(model_path, providers=providers)
            # --- 添加这两行进行诊断 ---
            current_provider = self.session.get_providers()[0]
            print(f"!!! 当前运行设备: {current_provider} (如果是 CPUExecutionProvider 说明没用到显卡) !!!")
            # -----------------------
        except Exception as e:
            print(f"模型加载失败: {e}")
            exit()

        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        # self.input_width = 640
        # self.input_height = 640
        self.input_width = 672
        self.input_height = 672  # 这是一个方形输入，虽然是 672x672

        # 加载类别名称
        a = []
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # 如果没有指定 names_path，尝试根据模型文件名自动推断，或者使用默认
        if names_path is None:
            if "baked1" in model_path:
                names_file_path = os.path.join(script_dir, "./v8_world/model_names1.txt")
            elif "baked2" in model_path:
                names_file_path = os.path.join(script_dir, "./v8_world/model_names2.txt")
            elif "baked3" in model_path:
                names_file_path = os.path.join(script_dir, "./v8_world/model_names3.txt")
            else:
                # 默认回退
                names_file_path = os.path.join(script_dir, "./v8_world/model_names2.txt")
        else:
            # 如果传入的是相对路径，相对于当前脚本目录；如果是绝对路径则直接使用
            if not os.path.isabs(names_path):
                names_file_path = os.path.join(script_dir, names_path)
            else:
                names_file_path = names_path

        print(f"正在加载标签文件: {names_file_path}")

        if os.path.exists(names_file_path):
            with open(names_file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    # 去除每行首尾的空格、换行符
                    line_stripped = line.strip()
                    # 过滤空行和注释行（以#开头的行）
                    if not line_stripped or line_stripped.startswith('#'):
                        continue
                    # 将有效类别添加到列表
                    a.append(line_stripped)
            self.classes = a
        else:
            print(f"警告: 找不到标签文件 {names_file_path}，将使用空类别列表")
            self.classes = []

    def preprocess(self, image):
        """
        改进版预处理：Letterbox (保持长宽比，填充黑边)
        """
        self.img_height, self.img_width = image.shape[:2]

        # 计算缩放比例
        scale = min(self.input_width / self.img_width, self.input_height / self.img_height)
        new_w = int(self.img_width * scale)
        new_h = int(self.img_height * scale)

        # 记录缩放参数供后处理使用
        self.scale = scale
        self.pad_w = (self.input_width - new_w) // 2
        self.pad_h = (self.input_height - new_h) // 2

        # 1. Resize
        img = cv2.resize(image, (new_w, new_h))

        # 2. Convert Color
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        # 3. Pad (填充灰/黑边到 640x640)
        # color=(114,114,114) 是 YOLO 的标准填充色
        img = cv2.copyMakeBorder(img,
                                 self.pad_h, self.input_height - new_h - self.pad_h,
                                 self.pad_w, self.input_width - new_w - self.pad_w,
                                 cv2.BORDER_CONSTANT, value=(114, 114, 114))

        # 4. Normalize & Transpose
        input_img = img.astype(np.float32) / 255.0
        input_tensor = input_img.transpose(2, 0, 1)  # HWC -> CHW
        input_tensor = input_tensor[np.newaxis, :, :, :]  # -> BCHW

        return input_tensor

    def detect(self, image):
        # 1. 预处理 (这里会计算 self.scale, self.pad_w, self.pad_h)
        input_tensor = self.preprocess(image)

        # 2. 推理
        outputs = self.session.run([self.output_name], {self.input_name: input_tensor})

        # YOLOv8 输出转置
        preds = np.squeeze(outputs[0]).T

        boxes = []
        confidences = []
        class_ids = []

        # 过滤低置信度
        # 建议：如果是 1000 类模型，conf_threshold 至少设为 0.35 以减少乱跳
        scores = np.max(preds[:, 4:], axis=1)
        keep_idxs = scores > self.conf_threshold

        preds_keep = preds[keep_idxs]
        scores_keep = scores[keep_idxs]
        class_ids_keep = np.argmax(preds_keep[:, 4:], axis=1)

        for i, pred in enumerate(preds_keep):
            cx, cy, w, h = pred[0], pred[1], pred[2], pred[3]

            # === 坐标还原的核心逻辑 (Letterbox 逆变换) ===
            # 1. 减去黑边 (Padding)
            cx = cx - self.pad_w
            cy = cy - self.pad_h

            # 2. 除以缩放比例 (Scale)
            cx /= self.scale
            cy /= self.scale
            w /= self.scale
            h /= self.scale

            # 3. 转为左上角坐标 (xywh -> xyxy)
            left = int(cx - w / 2)
            top = int(cy - h / 2)
            width = int(w)
            height = int(h)

            boxes.append([left, top, width, height])
            confidences.append(float(scores_keep[i]))
            class_ids.append(int(class_ids_keep[i]))

        # NMS 去重
        # 如果觉得框重叠还是严重，可以把 0.45 改成 0.3
        indices = cv2.dnn.NMSBoxes(boxes, confidences, self.conf_threshold, self.iou_threshold)

        results = []
        if len(indices) > 0:
            for i in indices.flatten():
                results.append((class_ids[i], confidences[i], boxes[i]))

        return results
