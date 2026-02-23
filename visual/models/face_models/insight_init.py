from insightface.app import FaceAnalysis

# 全局单例模型加载
_app = None


def load_insightface_model():
    global _app
    if _app is None:
        print("正在加载 InsightFace 模型 (首次运行会自动下载模型)...")
        # name='buffalo_l' 是精度较高的模型 (推荐)
        # name='buffalo_s' 是速度极快的模型 (适合 CPU)
        # 配置使用 GPU 0（第一张 NVIDIA 显卡）
        # 注意：CUDAExecutionProvider 只会看支持 CUDA 的设备（NVIDIA 显卡），会自动忽略 Intel 核显
        # 在 CUDA 的眼中，RTX 4060 是它看到的第一张卡，ID 是 0
        # InsightFace 使用 ctx_id 参数指定 GPU：
        # ctx_id=-1 表示使用 CPU
        # ctx_id=0 表示使用第一张 NVIDIA 显卡 (GPU 0，如 RTX 4060)
        # 如果 CUDA 可用，使用 GPU 0；否则回退到 CPU
        try:
            import onnxruntime as ort
            # 检查 CUDA 是否可用
            available_providers = ort.get_available_providers()
            if 'CUDAExecutionProvider' in available_providers:
                # 使用 GPU 0，配置 ONNX Runtime 使用 device_id=0
                app = FaceAnalysis(name='buffalo_l', providers=[('CUDAExecutionProvider', {'device_id': 0}), 'CPUExecutionProvider'])
                # ctx_id=0 表示使用第一张 NVIDIA 显卡 (GPU 0)
                app.prepare(ctx_id=0, det_size=(640, 640))
                print("InsightFace 已配置使用 GPU 0")
            else:
                # CUDA 不可用，使用 CPU
                app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
                app.prepare(ctx_id=-1, det_size=(640, 640))
                print("InsightFace 已配置使用 CPU (CUDA 不可用)")
        except Exception as e:
            # 如果出错，回退到 CPU
            print(f"警告: GPU 初始化失败，回退到 CPU: {e}")
            app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
            app.prepare(ctx_id=-1, det_size=(640, 640))
        _app = app
        print("InsightFace 模型加载完成。")
    return _app
