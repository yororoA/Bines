#!/usr/bin/env python3
"""
实时流屏幕分析脚本：按固定间隔截屏，使用与 video_analysis_standalone 相同的 DashScope VLM
分析当前屏幕内容，将结果写入 server/realtime_screen_analysis.txt，供主模型作为系统消息注入。

不依赖项目内其他模块，单独运行。需设置环境变量 DASHSCOPE_API_KEY。
启用/关闭由 server/realtime_screen_config.json 的 enabled 控制。

用法:
  python realtime_screen_analysis_standalone.py
  python realtime_screen_analysis_standalone.py --interval 10
"""
import argparse
import base64
import io
import json
import os
import sys
import time
import ctypes
from ctypes import wintypes
from pathlib import Path

try:
    import requests
except ImportError:
    print("请安装 requests: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    import mss
    import imagehash
    from PIL import Image
except ImportError:
    print("请安装 mss, imagehash 与 pillow: pip install mss imagehash pillow", file=sys.stderr)
    sys.exit(1)

# PyAutoGUI 作为备选（获取鼠标位置用）
try:
    import pyautogui
except ImportError:
    pyautogui = None

# 项目根目录（脚本所在目录）
PROJECT_ROOT = Path(__file__).resolve().parent
SERVER_DIR = PROJECT_ROOT / "server"
CONFIG_PATH = SERVER_DIR / "realtime_screen_config.json"
OUTPUT_PATH = SERVER_DIR / "realtime_screen_analysis.txt"
PID_PATH = SERVER_DIR / "realtime_screen_pid.txt"
CONTEXT_PATH = SERVER_DIR / "realtime_screen_context.json"
LOG_PATH = SERVER_DIR / "realtime_screen_analysis.log"

# 将 stderr 同时写入日志文件，便于管理端启动时无控制台也能排查问题
class _Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, data):
        for f in self.files:
            try:
                f.write(data)
                f.flush()
            except Exception:
                pass
    def flush(self):
        for f in self.files:
            try:
                f.flush()
            except Exception:
                pass
try:
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = open(LOG_PATH, "a", encoding="utf-8")
    sys.stderr = _Tee(sys.__stderr__, _log_file)
except Exception:
    pass

# 与 video_analysis_standalone 一致的 DashScope 配置
DASHSCOPE_API_URL = os.environ.get(
    "DASHSCOPE_API_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
)
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")
DASHSCOPE_VISION_MODEL = os.environ.get("DASHSCOPE_VISION_MODEL", "qwen3-vl-flash")
DASHSCOPE_API_TIMEOUT = int(os.environ.get("DASHSCOPE_API_TIMEOUT", "60"))

# 截图长宽各压缩一半再上传，降低 API 体积
SCALE_HALF = 0.5
def get_active_window_rect():
    """使用 ctypes 获取当前活动窗口的坐标 (left, top, width, height)。"""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
            
        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            # Handle multi-monitor or off-screen coordinates if necessary, 
            # but mss handles screen coordinates well.
            x = rect.left
            y = rect.top
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            # Filter out invalid or tiny windows
            if w < 10 or h < 10:
                return None
            return {"top": y, "left": x, "width": w, "height": h}
    except Exception:
        pass
    return None

def load_config():
    """读取 realtime_screen_config.json，缺省为未启用、间隔 10 秒、变化阈值 0.15。"""
    if not CONFIG_PATH.exists():
        return {"enabled": False, "interval_sec": 10, "min_change_ratio": 0.15}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        ratio = float(data.get("min_change_ratio", 0.15))
        ratio = max(0.0, min(1.0, ratio))
        return {
            "enabled": bool(data.get("enabled", False)),
            "interval_sec": max(3, min(120, int(data.get("interval_sec", 10)))),
            "min_change_ratio": ratio,
        }
    except Exception:
        return {"enabled": False, "interval_sec": 10, "min_change_ratio": 0.15}


def compute_change_ratio(prev_pil, current_pil, sample_size=32):
    """
    计算两帧之间的变化程度 (0~1)。
    使用 Perceptual Hash (pHash) 判断语义相似度，并结合 ROI (中心/鼠标区域) 加权。
    返回值越高表示变化越大。
    """
    if prev_pil is None or current_pil is None:
        return 1.0
    
    try:
        # 1.pHash 差异 (语义层面)
        # imagehash.phash 默认生成 64 位 hash
        phash1 = imagehash.phash(prev_pil)
        phash2 = imagehash.phash(current_pil)
        # diff 是汉明距离 (0-64)，归一化到 0-1
        phash_diff = (phash1 - phash2) / 64.0

        # 2. ROI (Region of Interest) 简单加权
        # 如果鼠标在动，或者中心区域变化大，给予更高权重
        # 这里简化处理：截取中心 50% 区域再算一次 pHash
        w, h = current_pil.size
        box = (w * 0.25, h * 0.25, w * 0.75, h * 0.75)
        
        center_prev = prev_pil.crop(box)
        center_curr = current_pil.crop(box)
        
        phash1_center = imagehash.phash(center_prev)
        phash2_center = imagehash.phash(center_curr)
        center_diff = (phash1_center - phash2_center) / 64.0

        # 3. 鼠标位置加权 (如果有 pyautogui)
        mouse_weight = 1.0
        if pyautogui:
            try:
                mx, my = pyautogui.position()
                # 归一化鼠标坐标
                # 注意：current_pil 是缩放后的，这里用相对位置判断大致区域即可
                # 简单逻辑：如果鼠标移动了，且当前画面有变化，放大变化值
                # 这里暂不记录上一帧鼠标位置，仅作为"交互活跃"的潜在加分项(暂略)
                pass 
            except Exception:
                pass

        # 综合评分：中心区域权重更高 (0.6 * 全局 + 0.4 * 中心)
        # pHash 对微小噪点不敏感，主要反映结构变化
        final_change = 0.6 * phash_diff + 0.4 * center_diff
        
        return final_change

    except Exception as e:
        print(f"[DiffCheck Error] {e}", file=sys.stderr)
        return 1.0


def load_dialog_context():
    """读取主模型传入的对话上下文（用户输入 + 助手输出），用于 VLM 关注点。无文件或异常则返回 None。"""
    if not CONTEXT_PATH.exists():
        return None
    try:
        with open(CONTEXT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        user_input = (data.get("user_input") or "").strip()
        assistant_output = (data.get("assistant_output") or "").strip()
        if not user_input and not assistant_output:
            return None
        return {"user_input": user_input, "assistant_output": assistant_output}
    except Exception:
        return None


def _focus_prefix(context):
    """根据对话上下文生成「关注主体」提示前缀。"""
    if not context or (not context.get("user_input") and not context.get("assistant_output")):
        return ""
    parts = []
    if context.get("user_input"):
        parts.append(f"用户说：{context['user_input']}")
    if context.get("assistant_output"):
        parts.append(f"助手回复：{context['assistant_output']}")
    return (
        "【当前对话】\n" + "\n".join(parts) + "\n\n"
        "请结合上述对话，重点关注与用户意图或助手回复相关的内容（如用户提到的界面、助手建议的操作、当前应关注的区域等）。\n\n"
    )


def capture_screen_resized(scale=SCALE_HALF, quality=85):
    """
    使用 mss 截取主屏并缩放到 scale 倍，返回 PIL Image。
    mss 在 Windows 上通常比 pyautogui 快 10 倍以上。
    """
    try:
        with mss.mss() as sct:
            # monitor 1 是主显示器. 如果只有一个显示器，monitors[1] 就是它（monitors[0]是所有显示器的组合）
            if len(sct.monitors) > 1:
                monitor = sct.monitors[1]
            else:
                monitor = sct.monitors[0]
            
            sct_img = sct.grab(monitor)
            
            # mss 返回的是 BGRA，转换为 RGB
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            
            w, h = img.size
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            
            if (new_w, new_h) != (w, h):
                img = img.resize((new_w, new_h), getattr(Image, "Resampling", Image).LANCZOS)
            
            return img
    except Exception as e:
        print(f"[ScreenCapture Error] {e}", file=sys.stderr)
        # Fallback to pyautogui if mss fails
        if pyautogui:
             try:
                 return pyautogui.screenshot()
             except:
                 pass
        return None

def pil_to_base64(img, format="JPEG", quality=75):
    """将 PIL Image 转换为 Base64 字符串"""
    try:
        buff = io.BytesIO()
        # 转换为 RGB 避免 RGBA 保存为 JPEG 报错
        if img.mode == "RGBA":
            img = img.convert("RGB")
        elif img.mode != "RGB":
            # 保持灰度或转为 RGB
            pass
        img.save(buff, format=format, quality=quality)
        return base64.b64encode(buff.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"[ImageEnc Error] {e}", file=sys.stderr)
        return ""

def call_dashscope_vlm(image_b64_list, prompt: str) -> str:
    """
    调用 DashScope 视觉模型，返回文本描述。
    支持传入单张图片 base64 字符串，或图片 base64 列表 (多图模式)。
    """
    if not DASHSCOPE_API_KEY:
        return "[未设置 DASHSCOPE_API_KEY]"
    
    # 兼容旧调用：如果是字符串，转为列表
    if isinstance(image_b64_list, str):
        image_b64_list = [image_b64_list]
        
    url = DASHSCOPE_API_URL
    
    # 构建 content 列表
    content_list = [{"type": "text", "text": prompt}]
    for i, img_b64 in enumerate(image_b64_list):
        if i == 0:
             content_list.append({"type": "text", "text": "图1：全屏概览"})
        elif i == 1:
             content_list.append({"type": "text", "text": "图2：当前活动窗口高分辨率详情"})
             
        content_list.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})

    payload = {
        "model": DASHSCOPE_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": content_list
            }
        ],
    }
    
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=DASHSCOPE_API_TIMEOUT,
            proxies={"http": None, "https": None},
        )
        if resp.status_code != 200:
            return f"[VLM 请求失败 {resp.status_code}] {resp.text[:200]}"
        out = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return out or "[VLM 返回为空]"
    except Exception as e:
        return f"[VLM 异常] {e}"


def build_composite_left_to_right(im1, im2, im3):
    """将三张图按左→右拼接为一张（左=最早，右=当前），用于动作变化识别。三图尺寸需一致。"""
    w, h = im1.size
    composite = Image.new("RGB", (w * 3, h))
    composite.paste(im1, (0, 0))
    composite.paste(im2, (w, 0))
    composite.paste(im3, (w * 2, 0))
    return composite

def build_prompt(context=None) -> str:
    """单帧：当前屏幕分析提示。若有 context（用户输入+助手输出）则前置关注点说明。"""
    base = (
        "请用尽量简洁的中文概括当前屏幕（2–4 句话内）：当前窗口/应用、主要界面与关键文字、整体场景。"
        "不要列点，不要冗长，适合作为助手理解用户当前在做什么的简短上下文。"
        "若画面是代码/编辑器/IDE 等编程相关场景，直接输出「正在编写代码」即可，不要对代码内容做任何描述。"
    )
    return _focus_prefix(context) + base


def build_change_prompt(context=None) -> str:
    """三帧拼接（左→右=时间顺序）：让模型识别发生了什么变化。若有 context 则前置关注点说明。"""
    base = (
        "本图由同一屏幕连续三帧从左到右拼接（左最早，右最新）。"
        "请用尽量简洁的中文概括（2–4 句话）：当前画面主要内容 + 从左到右发生了哪些变化（如点击、输入、切换、滚动等）。"
        "不要列点，不要冗长。"
        "若画面是代码/编辑器/IDE 等编程相关场景，直接输出「正在编写代码」即可，不要对代码内容做任何描述。"
    )
    return _focus_prefix(context) + base


def capture_active_window(rect):
    """
    截取指定区域 (活动窗口)，尽量保持原始分辨率 (不缩放)，用于看清细节。
    """
    if not rect:
        return None
    try:
        with mss.mss() as sct:
            # rect 是 {'top': y, 'left': x, 'width': w, 'height': h}
            sct_img = sct.grab(rect)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            return img
    except Exception:
        return None


# 缓冲机制配置
BUFFER_MAX_SIZE = 5         # 最大缓冲条数
BUFFER_FLUSH_INTERVAL = 30  # 最大缓冲时间（秒），超过则强制推送
FLUSH_SIGNAL_PATH = SERVER_DIR / "realtime_screen_flush.signal" # 强制刷新信号文件

def check_flush_condition(buffer, context, last_flush_time, last_signal_mtime):
    """
    判断是否满足推送缓冲区的条件。
    条件：
    1. 用户有输入 (context['user_input'] 非空) -> 立即推送
    2. LLM 有明确关注指令 (context['assistant_output'] 非空且包含特定指令?) -> 简化为有输出就推
    3. 收到外部 flush 信号 (bored 触发时写入此文件) -> 强制推送
    4. 缓冲超时 -> 强制推送 (避免太久没反应)
    5. 缓冲区满了 -> 强制推送
    
    Returns:
        (bool, str, float): (是否推送, 原因, 新的signal_mtime)
    """
    current_signal_mtime = last_signal_mtime
    has_signal = False
    
    # 检查 flush 信号文件
    try:
        if FLUSH_SIGNAL_PATH.exists():
            mtime = FLUSH_SIGNAL_PATH.stat().st_mtime
            if mtime > last_signal_mtime:
                has_signal = True
                current_signal_mtime = mtime
    except Exception:
        pass
        
    if has_signal:
        return True, "External Flush Signal (Bored)", current_signal_mtime

    has_user_input = bool(context and context.get("user_input"))
    has_assistant_focus = bool(context and context.get("assistant_output"))
    
    if has_user_input or has_assistant_focus:
        return True, "User/Assistant Interaction", current_signal_mtime
        
    if time.time() - last_flush_time > BUFFER_FLUSH_INTERVAL:
        return True, "Timeout", current_signal_mtime
        
    if len(buffer) >= BUFFER_MAX_SIZE:
        return True, "Buffer Full", current_signal_mtime
        
    return False, None, current_signal_mtime


def atomic_write(content, path):
    """
    原子写入文件，带重试机制以应对 Windows 文件锁 (PermissionError)。
    """
    temp_path = path.parent / (path.name + ".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
        
        # 重试 3 次，每次间隔 0.1s
        for i in range(5):
            try:
                if path.exists():
                     path.unlink() # 尝试显式删除目标文件 (有时 replace 会失败)
                os.replace(temp_path, path)
                return True
            except PermissionError:
                time.sleep(0.2)
                continue
            except OSError:
                # 其他 OS 错误可能无法恢复
                break
    except Exception:
        pass
    
    # 清理临时文件
    try:
        if temp_path.exists():
            temp_path.unlink()
    except Exception:
        pass
    return False

def run_loop():
    """主循环：按间隔读配置，启用时截屏→VLM→写文件；禁用时写回 PID 并退出，便于管理端启停。"""
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    last_enabled = False
    prev_frames = []
    
    # 新增：本地缓冲区
    analysis_buffer = [] 
    last_flush_time = time.time()
    last_signal_mtime = 0

    while True:
        config = load_config()
        if not config["enabled"]:
            if last_enabled:
                print("[RealtimeScreen] 已暂停实时屏幕分析（等待重新启用）...", file=sys.stderr, flush=True)
                try:
                    if PID_PATH.exists():
                        PID_PATH.unlink()
                except Exception:
                    pass
            
            last_enabled = False
            prev_frames = []
            analysis_buffer = [] # 清空缓冲
            time.sleep(5)  # 休眠5秒后再次检查配置
            continue

        if not last_enabled:
            print(f"[RealtimeScreen] 已启用，间隔 {config['interval_sec']} 秒", file=sys.stderr, flush=True)
            try:
                PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
            except Exception:
                pass
        last_enabled = True
        interval = config["interval_sec"]
        min_change_ratio = config.get("min_change_ratio", 0.15)

        try:
            context = load_dialog_context()
            
            # 1. 获取全屏概览（缩放版）
            current_pil = capture_screen_resized(scale=SCALE_HALF)
            
            if current_pil is None:
                print("[RealtimeScreen] 截图失败，跳过本帧", file=sys.stderr, flush=True)
                time.sleep(interval)
                continue

            # 2. 计算变化 diff (基于全屏概览)
            prev_pil = prev_frames[-1] if len(prev_frames) >= 1 else None
            change_ratio = compute_change_ratio(prev_pil, current_pil)

            # 变化不足，跳过 VLM
            # 但如果有用户输入，强制分析一次 (忽略 min_change_ratio)
            has_interaction = bool(context and (context.get("user_input") or context.get("assistant_output")))
            
            # 检查是否有外部强制 flush 信号 (即使画面没变，如果无聊了也可能强制触发?)
            # 但这里逻辑是先决定是否 analyze (call VLM)，然后再决定是否 flush buffer.
            # 如果画面没变跳过了 analyze，buffer 里就没有新东西。
            # 但是如果 Bored 触发，通常意味着用户没动，画面也没动。
            # 这时也许应该强制 analyze 一次? 
            # 暂时保持原逻辑：画面没变就不 analyze。
            # "Bored" event might want to flush whatever is currently in buffer.
            
            # Additional check: If flush signal is present and new, maybe we should force analyze regardless of visual change?
            # For now, let's respect min_change_ratio. If screen is static, buffer is static.
            # But the user might want to know "screen is static" if they ask "what happened".
            
            if not has_interaction and min_change_ratio > 0 and prev_pil is not None and change_ratio < min_change_ratio:
                 print(f"[RealtimeScreen] 变化 {change_ratio:.2%} < {min_change_ratio:.2%}，跳过", file=sys.stderr, flush=True)
                 
                 # Check flush just in case we have pending buffer?
                 should_flush, reason, new_signal_mtime = check_flush_condition(analysis_buffer, context, last_flush_time, last_signal_mtime)
                 if should_flush and analysis_buffer:
                     final_output = "\n".join(analysis_buffer)
                     if len(analysis_buffer) > 1:
                         final_output = f"【近期屏幕活动记录 (Reason: {reason})】\n" + final_output

                     atomic_write(final_output, OUTPUT_PATH)
                     
                     print(f"[RealtimeScreen] 推送分析 (SKIP状态) ({len(analysis_buffer)} 条, Reason: {reason})", file=sys.stderr, flush=True)
                     analysis_buffer = []
                     last_flush_time = time.time()
                     last_signal_mtime = new_signal_mtime
                     
                 prev_frames = (prev_frames + [current_pil.copy()])[-2:]
                 time.sleep(interval)
                 continue

            # 3. 准备图片列表
            imgs_to_send = []
            
            # Part A: 全屏图 (或拼接图)
            if len(prev_frames) >= 2:
                composite = build_composite_left_to_right(
                    prev_frames[-2], prev_frames[-1], current_pil
                )
                img_b64_full = pil_to_base64(composite)
                prompt = build_change_prompt(context)  # 提示词强调动作变化
            else:
                img_b64_full = pil_to_base64(current_pil)
                prompt = build_prompt(context)         # 提示词强调当前状态

            imgs_to_send.append(img_b64_full)
            
            # Part B: 活动窗口高清图 (仅当前帧)
            active_rect = get_active_window_rect()
            active_window_pil = capture_active_window(active_rect)
            if active_window_pil:
                # 限制一下最大尺寸，避免 4K 全屏窗口直接发过去太大
                max_dim = 1200
                w, h = active_window_pil.size
                if w > max_dim or h > max_dim:
                    active_window_pil.thumbnail((max_dim, max_dim), getattr(Image, "Resampling", Image).LANCZOS)
                
                img_b64_active = pil_to_base64(active_window_pil)
                
                # 如果活动窗口太小（比如只是个确认框），可能没必要单独发，或者全屏已经看清了
                # 这里简单策略：只要获取到了就发
                imgs_to_send.append(img_b64_active)
                prompt += r"\n(附图2为当前活动窗口的高清细节，请结合全屏概览与局部细节进行分析。)"
            
            # 4. 调用 VLM
            desc = call_dashscope_vlm(imgs_to_send, prompt)
            
            if not desc:
                desc = "[本次未得到有效描述]"
                
            # 5. 缓冲逻辑
            timestamp = time.strftime("%H:%M:%S")
            analysis_buffer.append(f"[{timestamp}] {desc}")
            
            should_flush, reason, new_signal_mtime = check_flush_condition(analysis_buffer, context, last_flush_time, last_signal_mtime)
            
            if should_flush:
                last_signal_mtime = new_signal_mtime
                # 拼接缓冲内容
                final_output = "\n".join(analysis_buffer)
                if len(analysis_buffer) > 1:
                    final_output = f"【近期屏幕活动记录 (Reason: {reason})】\n" + final_output
                
                # 原子写入
                atomic_write(final_output, OUTPUT_PATH)
                print(f"[RealtimeScreen] 推送分析 ({len(analysis_buffer)} 条, Reason: {reason})", file=sys.stderr, flush=True)
                
                # 清空缓冲
                analysis_buffer = []
                last_flush_time = time.time()
            else:
                print(f"[RealtimeScreen] 已缓冲 ({len(analysis_buffer)}/{BUFFER_MAX_SIZE})", file=sys.stderr, flush=True)
                
            prev_frames = (prev_frames + [current_pil.copy()])[-2:]
            
        except Exception as e:
            print(f"[RealtimeScreen] 分析异常: {e}", file=sys.stderr, flush=True)
            try:
                atomic_write(f"[分析异常] {e}", OUTPUT_PATH)
            except Exception:
                pass

        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(
        description="实时屏幕分析：按间隔截屏并用 DashScope VLM 分析，结果写入 server/realtime_screen_analysis.txt"
    )
    parser.add_argument(
        "--interval", "-i", type=int, default=None,
        help="分析间隔（秒），未指定则使用 server/realtime_screen_config.json 中的配置",
    )
    args = parser.parse_args()

    if args.interval is not None:
        # 命令行覆盖：临时写入 config 的 interval，enabled 仍从 config 读
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        data["interval_sec"] = max(3, min(120, args.interval))
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    print("[RealtimeScreen] 启动，配置来自", CONFIG_PATH, file=sys.stderr, flush=True)
    run_loop()


if __name__ == "__main__":
    main()
