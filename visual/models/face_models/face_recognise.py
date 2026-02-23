import cv2
import numpy as np
import pickle
import os
from .insight_init import load_insightface_model

# 加载模型
app = load_insightface_model()

# 相似度阈值 (余弦相似度)
# InsightFace 中，0.5 左右通常是一个较好的判定阈值
THRESHOLD = 0.4


class FaceUser:
    def __init__(self, name: str, embedding):
        self.name = name
        self.embedding = embedding


inputed = []  # 已录入人脸数据库

def save_embeddings(path="face_db.pkl"):
    try:
        with open(path, "wb") as f:
            pickle.dump(inputed, f)
        print(f"人脸库已保存至: {path} (共 {len(inputed)} 人)")
    except Exception as e:
        print(f"保存人脸库失败: {e}")

def load_embeddings(path="face_db.pkl"):
    global inputed
    if not os.path.exists(path):
        print("未找到人脸库缓存，将重新初始化。")
        return False
    try:
        with open(path, "rb") as f:
            inputed = pickle.load(f)
        print(f"成功加载人脸库: {path} (共 {len(inputed)} 人)")
        return True
    except Exception as e:
        print(f"加载人脸库失败: {e}")
        return False

# --- 辅助函数：计算余弦相似度 ---
def compute_sim(feat1, feat2):
    return np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))


# --- 人脸查找 (仅返回坐标，为了兼容旧接口) ---
def face_find(frame, find: bool = True):
    # InsightFace 接受 BGR 图片，无需转换
    faces = app.get(frame)

    if find:
        face_sites = []
        for face in faces:
            # bbox 是 [x1, y1, x2, y2] 的浮点数数组
            box = face.bbox.astype(int)
            face_sites.append(box.tolist())
        return face_sites
    else:
        # 如果 find=False，执行识别并绘制 (调试用)
        # 这里简单复用 identify 的逻辑
        results = face_identify(frame)
        for res in results:
            (startX, startY, endX, endY) = res[0]
            name = res[1]
            cv2.rectangle(frame, (startX, startY), (endX, endY), (255, 0, 0), 2)
            cv2.putText(frame, name, (startX, startY - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        return frame


# --- 人脸录入 ---
def face_input(image, name):
    print(f"正在录入: {name}...")
    # app.get 会同时进行检测、关键点定位和特征提取
    faces = app.get(image)

    if len(faces) > 1:
        print(f"[{name}] 录入失败: 图片中检测到多张人脸，请使用单人照。")
        return
    elif len(faces) == 0:
        print(f"[{name}] 录入失败: 未检测到人脸。")
        return

    # 获取特征向量 (embedding)
    # embedding 是一个 512 维的 numpy 数组
    face_embedding = faces[0].embedding

    inputed.append(FaceUser(name=name, embedding=face_embedding))
    print(f"成功录入: {name}")


# --- 人脸身份识别 ---
def face_identify(image):
    """
    返回格式: [ [[x1, y1, x2, y2], "Name"], ... ]
    """
    # InsightFace 速度很快，通常不需要像 Dlib 那样缩小图片
    # 但如果为了极致帧率，可以将图片缩小，识别完再把坐标乘回去
    # 这里为了代码简洁和精度，直接使用原图

    faces = app.get(image)
    sites_names = []

    if len(faces) == 0:
        return sites_names

    for face in faces:
        # 获取当前人脸的特征
        current_embedding = face.embedding
        box = face.bbox.astype(int).tolist()  # [x1, y1, x2, y2]

        max_sim = 0
        pred_name = "Unknown"

        # 与数据库中的人脸比对
        if len(inputed) > 0:
            for user in inputed:
                sim = compute_sim(current_embedding, user.embedding)
                if sim > 0.3:
                    print(f"与 [{user.name}] 的相似度: {sim:.4f}")
                if sim > max_sim:
                    max_sim = sim
                    if sim >= THRESHOLD:
                        pred_name = user.name

        sites_names.append([box, pred_name])

    return sites_names