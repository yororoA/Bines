#!/usr/bin/env python3
"""
独立视频分析脚本：使用与屏幕分析相同的 DashScope VLM（如 qwen3-vl-flash），
按 5 秒一段对视频进行切分并分析，输出人物表情/动作/环境/交互的连贯叙述。

不依赖项目内任何模块，单独运行即可。需设置环境变量 DASHSCOPE_API_KEY。

用法:
  python video_analysis_standalone.py --video path/to/video.mp4
  python video_analysis_standalone.py --video path/to/video.mp4 --interval 5 --output result.txt
"""
import argparse
import base64
import io
import os
import sys

# 仅使用标准库 + requests，视频用 opencv
try:
    import cv2
except ImportError:
    print("请安装 opencv-python: pip install opencv-python", file=sys.stderr)
    sys.exit(1)
try:
    import requests
except ImportError:
    print("请安装 requests: pip install requests", file=sys.stderr)
    sys.exit(1)

# 与项目屏幕分析一致的 DashScope 配置（仅从环境变量读取，不引用 config）
DASHSCOPE_API_URL = os.environ.get(
    "DASHSCOPE_API_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
)
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")
DASHSCOPE_VISION_MODEL = os.environ.get("DASHSCOPE_VISION_MODEL", "qwen3-vl-flash")
DASHSCOPE_API_TIMEOUT = int(os.environ.get("DASHSCOPE_API_TIMEOUT", "60"))

# 输出图片最大边，控制 API 体积
MAX_IMAGE_SIZE = 1280


def get_frames_by_interval(video_path: str, interval_sec: float):
    """
    按固定秒数间隔从视频中抽取一帧。
    返回: [(时间秒数, BGR 图像), ...]
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if total_frames else 0
    cap.release()

    if duration_sec <= 0:
        return []

    result = []
    t = 0.0
    cap = cv2.VideoCapture(video_path)
    try:
        while t < duration_sec:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            result.append((round(t, 1), frame.copy()))
            t += interval_sec
    finally:
        cap.release()
    return result


def frame_to_base64_jpeg(frame, max_size=MAX_IMAGE_SIZE, quality=85):
    """将 BGR 帧转为 JPEG base64，可选缩放以控制大小。"""
    h, w = frame.shape[:2]
    if max(w, h) > max_size:
        r = max_size / max(w, h)
        new_w, new_h = int(w * r), int(h * r)
        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def call_dashscope_vlm(image_b64: str, prompt: str) -> str:
    """调用 DashScope 视觉模型，返回文本描述。与屏幕分析使用同一 API 形态。"""
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("请设置环境变量 DASHSCOPE_API_KEY")
    url = DASHSCOPE_API_URL
    image_content = f"data:image/jpeg;base64,{image_b64}"
    payload = {
        "model": DASHSCOPE_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_content}},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=DASHSCOPE_API_TIMEOUT,
        proxies={"http": None, "https": None},
    )
    if resp.status_code != 200:
        return f"[VLM 请求失败 {resp.status_code}] {resp.text[:300]}"
    try:
        return (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    except (IndexError, KeyError, TypeError):
        return "[VLM 返回解析错误]"


def build_prompt(segment_index: int, total_segments: int, time_sec: float, prev_summary: str | None) -> str:
    """构建本段的分析提示：含上一段概要时要求先承接再写本段。"""
    base = (
        "请分析本画面（视频中的一帧），按以下维度用连贯的中文叙述，不要列点：\n"
        "1. 人物表情与情绪\n"
        "2. 人物动作与姿态\n"
        "3. 环境与场景\n"
        "4. 人物之间或人物与环境的交互\n"
        "叙述请自然连贯，适合作为视频解说文案。"
    )
    if prev_summary:
        base = (
            "【上一段内容概要】\n"
            f"{prev_summary}\n\n"
            "请先用一两句话承接上文（说明与上一段的衔接），再对本段画面进行同样维度的分析：人物表情/动作、环境、交互。保持整体叙述连贯。\n\n"
            + base
        )
    base += f"\n\n（本段为第 {segment_index + 1}/{total_segments} 段，对应时间约 {time_sec} 秒。）"
    return base


def run(video_path: str, interval_sec: float = 5.0, output_path: str | None = None):
    frames = get_frames_by_interval(video_path, interval_sec)
    if not frames:
        print("未抽取到任何帧，请检查视频路径与格式。", file=sys.stderr)
        return
    total = len(frames)
    print(f"视频共抽取 {total} 段（每 {interval_sec} 秒一帧），开始逐段分析...", file=sys.stderr)

    lines = []
    prev_summary = None
    for i, (t_sec, frame) in enumerate(frames):
        print(f"  [{i+1}/{total}] 分析 {t_sec}s ...", file=sys.stderr, flush=True)
        prompt = build_prompt(i, total, t_sec, prev_summary)
        img_b64 = frame_to_base64_jpeg(frame)
        desc = call_dashscope_vlm(img_b64, prompt)
        if not desc:
            desc = f"[本段 {t_sec}s 未得到有效描述]"
        # 用于下一段的「上一段概要」：取本段描述的前 200 字或整段（若较短）
        prev_summary = desc[:200] + "..." if len(desc) > 200 else desc
        lines.append(f"【{t_sec}秒】\n{desc}\n")
        print(f"     -> 已得到 {len(desc)} 字", file=sys.stderr, flush=True)

    full_text = "\n".join(lines)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(full_text)
        print(f"已写入: {output_path}", file=sys.stderr)
    print("\n" + "=" * 60 + "\n")
    print(full_text)


def main():
    parser = argparse.ArgumentParser(
        description="独立视频分析：按固定间隔切帧，用 DashScope VLM 分析人物表情/动作/环境/交互，输出连贯叙述。"
    )
    parser.add_argument("--video", "-v", required=True, help="视频文件路径")
    parser.add_argument("--interval", "-i", type=float, default=5.0, help="分析间隔（秒），默认 5")
    parser.add_argument("--output", "-o", default=None, help="结果输出到该文本文件（不指定则只打印到 stdout）")
    args = parser.parse_args()
    if not os.path.isfile(args.video):
        print(f"文件不存在: {args.video}", file=sys.stderr)
        sys.exit(1)
    run(args.video, args.interval, args.output)


if __name__ == "__main__":
    main()
