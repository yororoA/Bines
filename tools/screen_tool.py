import base64
import requests
import io
import time
import os
import json
import threading
from pathlib import Path
from config import (
    DASHSCOPE_API_URL,
    DASHSCOPE_API_KEY,
    DASHSCOPE_VISION_MODEL,
    DASHSCOPE_API_TIMEOUT,
    require_env,
)

# 实时屏幕分析配置路径
SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
REALTIME_CONFIG_PATH = SERVER_DIR / "realtime_screen_config.json"
REALTIME_OUTPUT_PATH = SERVER_DIR / "realtime_screen_analysis.txt"

# 全局锁，防止屏幕分析并发调用
_screen_analysis_lock = threading.Lock()
_screen_analysis_in_progress = False

def get_screen_info(simple_recognition=True, only_mouse_area=False, focus_description=None, fast_mode=False):
    """
    截图并进行视觉分析 - 包含网格辅助定位系统
    专为多显示器优化：强制只截取主显示屏
    
    Args:
        simple_recognition (bool): 是否是单纯的屏幕图像识别（不带鼠标标记）
        only_mouse_area (bool): 当 simple_recognition=False 时生效。是否裁切并只解析鼠标周围的内容。
        focus_description (str, optional): 指示模型重点关注和解析的内容。
        fast_mode (bool): 快速模式，进一步降低图片质量和尺寸以加快速度。
    """
    global _screen_analysis_in_progress

    # 检查实时屏幕分析是否启用
    # 如果 simple_recognition=True（只需文本描述），且实时分析已启用，则直接复用其实时结果
    # 避免额外的截图和 API 调用开销
    if simple_recognition and REALTIME_CONFIG_PATH.exists():
        try:
            with open(REALTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
            
            if config.get("enabled", False):
                # 实时分析已启用，尝试读取最新结果
                if REALTIME_OUTPUT_PATH.exists():
                    try:
                        with open(REALTIME_OUTPUT_PATH, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                        if content:
                            return f"[系统提示：实时屏幕分析已启用，直接返回最近一次分析结果]\n{content}"
                    except Exception:
                        pass
        except Exception:
            pass  # 读取配置失败，继续执行常规截图流程
    
    # 【修复】使用锁防止并发调用
    if not _screen_analysis_lock.acquire(blocking=False):
        # 如果已经有屏幕分析在进行，返回提示信息
        return "Error: 屏幕分析正在进行中，请稍后再试。如果持续出现此问题，可能是系统繁忙。"
    
    try:
        # 检查是否已有分析在进行
        if _screen_analysis_in_progress:
            return "Error: 屏幕分析正在进行中，请稍后再试。"
        
        _screen_analysis_in_progress = True
        
        try:
            import pyautogui
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return "Error: Missing dependencies. Please install 'pyautogui' and 'pillow' in the thinking environment."

        # 1. 获取主屏幕逻辑尺寸
        # pyautogui.size() 返回的是主显示器的分辨率
        logical_width, logical_height = pyautogui.size()
        
        # 2. 【修复】全屏截图后裁剪，避免高 DPI/多屏下的截图偏移问题
        # 在 Windows 高 DPI 环境下，region 参数的行为可能不一致
        # 改为全屏截图后手动裁剪，确保坐标一致性
        screenshot = pyautogui.screenshot()
        
        #【修复】智能处理 DPI 缩放与多显示器
        # 计算逻辑分辨率和物理分辨率的长宽比
        logical_ratio = logical_width / logical_height
        physical_ratio = screenshot.width / screenshot.height
        
        # 允许微小的浮点误差
        if abs(logical_ratio - physical_ratio) < 0.05:
            # 长宽比基本一致，说明是 DPI 缩放 (如 1920x1080 -> 1280x720)
            # 此时应该进行 RESIZE，将物理像素压缩回逻辑像素，以便 Grid 坐标与 pyautogui 坐标一致
            if screenshot.size != (logical_width, logical_height):
                print(f"[Screen Tool] 检测到DPI缩放 ({screenshot.size} -> {logical_width}x{logical_height})，执行缩放对齐")
                screenshot = screenshot.resize((logical_width, logical_height), resample=Image.LANCZOS)
        else:
            # 长宽比不一致，说明可能包含多显示器 (如双屏 3840x1080 vs 1920x1080)
            # 此时保留主屏幕区域 (通常是从 0,0 开始)
            print(f"[Screen Tool] 检测到多显示器/比例不一致 ({screenshot.size})，执行裁剪")
            screenshot = screenshot.crop((0, 0, logical_width, logical_height))

        # 处理 DPI 缩放带来的尺寸不一致问题
        # 确保最终截图尺寸与逻辑尺寸一致
        if screenshot.size != (logical_width, logical_height):
            print(f"[Screen Tool] 调整DPI: 截图{screenshot.size} -> 逻辑{logical_width}x{logical_height}")
            screenshot = screenshot.resize((logical_width, logical_height), resample=Image.LANCZOS)
        
        width, height = screenshot.size
        
        # 保存裁剪偏移量（在裁剪前记录）
        crop_offset_x = 0
        crop_offset_y = 0
        
        # 3. 处理 Prompt 和 局部裁剪
        # 新规则：
        # - 如果调用方提供了 focus_description，则将其视为完整的提示词，不再在此处固定模板；
        # - 如果未提供，则使用默认模板构造 prompt_text 作为兜底。
        prompt_text = ""
        
        # 如果调用方已经提供了完整的任务说明，则直接使用
        if focus_description:
            prompt_text = str(focus_description).strip()
        else:
            if simple_recognition:
                # 模式 A: 单纯屏幕识别（默认提示）
                prompt_text = "简要描述屏幕内容，重点关注图像形状和颜色。"
            else:
                # 模式 B: 结合鼠标信息或操作需求（默认提示）
                try:
                    mouse_x, mouse_y = pyautogui.position()
                    
                    # 【修复】确保鼠标位置在主屏幕范围内（处理多显示器情况）
                    mouse_x = max(0, min(mouse_x, width - 1))
                    mouse_y = max(0, min(mouse_y, height - 1))
                    
                    if only_mouse_area:
                        # 模式 B2: 只解析鼠标周围 (局部裁剪)
                        crop_size = 600
                        # 确保裁剪框不超出主屏幕范围
                        left = max(0, min(mouse_x - crop_size // 2, width - crop_size))
                        top = max(0, min(mouse_y - crop_size // 2, height - crop_size))
                        right = left + crop_size
                        bottom = top + crop_size
                        
                        # 记录裁剪偏移量
                        crop_offset_x = left
                        crop_offset_y = top
                        
                        screenshot = screenshot.crop((left, top, right, bottom))
                        
                        # 在裁剪后的图中画中心点示意
                        draw = ImageDraw.Draw(screenshot)
                        cx, cy = screenshot.size[0] // 2, screenshot.size[1] // 2
                        draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline="red", width=2)
                        
                        prompt_text = "这是鼠标周围区域(中心红圈为鼠标位置)。简要解析内容，重点识别文字和控件。"
                        
                    else:
                        # 模式 B1: 全屏带鼠标标记
                        draw = ImageDraw.Draw(screenshot)
                        r = 20
                        # 确保画圈坐标在有效范围内
                        mx = max(0, min(mouse_x, width))
                        my = max(0, min(mouse_y, height))
                        draw.ellipse((mx - r, my - r, mx + r, my + r), outline="red", width=3)
                        draw.ellipse((mx - 3, my - 3, mx + 3, my + 3), fill="red")
                        
                        prompt_text = "简要描述屏幕内容，提取文字信息。红色圆圈标记鼠标位置，分析用户可能关注的内容。"

                except Exception as e:
                    print(f"Warning: Failed to process mouse info: {e}")
                    prompt_text = "简要描述屏幕内容，重点提取文字信息。"

        # 4. 压缩与网格处理（全屏截取之后压缩为固定分辨率）
        if fast_mode:
            target_width, target_height = 952, 616  # 快速模式
            jpeg_quality = 85 if not simple_recognition else 60
        else:
            target_width, target_height = 1288, 812  # 普通模式
            jpeg_quality = 85
        
        original_width = screenshot.width
        original_height = screenshot.height
        
        # 计算缩放比例（用于网格字体大小）
        expected_scale_x = screenshot.width / target_width
        expected_scale_y = screenshot.height / target_height
        
        # 【核心】在原始尺寸上添加坐标网格（仅在需要操作坐标时）
        if not simple_recognition:
            font_scale = max(expected_scale_x, expected_scale_y)
            screenshot = _add_grid_overlay(screenshot, font_scale=font_scale,
                                         crop_offset_x=crop_offset_x, crop_offset_y=crop_offset_y)
            if crop_offset_x != 0 or crop_offset_y != 0:
                prompt_text += f"\n\n[重要提示] 图片上已覆盖了红色的坐标网格（带数字标签）。网格上的数字直接代表屏幕的真实像素坐标（已考虑裁剪偏移），请直接读取这些数字作为坐标值。例如，如果网格显示'500'，则坐标就是500。注意：这是裁剪后的区域，坐标范围从({crop_offset_x}, {crop_offset_y})开始。"
            else:
                prompt_text += "\n\n[重要提示] 图片上已覆盖了红色的坐标网格（带数字标签）。网格上的数字直接代表屏幕的真实像素坐标，请直接读取这些数字作为坐标值。例如，如果网格显示'500'，则坐标就是500。"
        
        # 全屏截取之后压缩分辨率：普通模式 1288x812，快速模式 952x616
        if (screenshot.width, screenshot.height) != (target_width, target_height):
            screenshot = screenshot.resize((target_width, target_height), resample=Image.LANCZOS)
        scale_x = original_width / screenshot.width
        scale_y = original_height / screenshot.height

        buff = io.BytesIO()
        screenshot.save(buff, format="JPEG", quality=jpeg_quality, optimize=True)
        img_b64 = base64.b64encode(buff.getvalue()).decode('utf-8')
        
        # 5. 调用 DashScope VLM
        url = DASHSCOPE_API_URL
        api_key = require_env("DASHSCOPE_API_KEY", DASHSCOPE_API_KEY)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        image_content = f"data:image/jpeg;base64,{img_b64}"
        
        # 提示词：明确网格数字就是真实坐标
        scale_info = ""
        if not simple_recognition:
            # 网格数字直接代表真实屏幕坐标，无需任何转换
            if crop_offset_x != 0 or crop_offset_y != 0:
                # 【修复】裁剪模式下，显示实际的坐标范围
                max_x = crop_offset_x + original_width
                max_y = crop_offset_y + original_height
                scale_info = f"""重要：图片上的红色网格数字直接代表屏幕的真实像素坐标。
请直接读取网格上的数值作为坐标，无需任何缩放或转换计算。
例如：如果网格显示"500"，则坐标就是500（真实屏幕像素）。
注意：这是裁剪后的区域，坐标范围：x={crop_offset_x}到{max_x}, y={crop_offset_y}到{max_y}。请使用格式"坐标: x, y"。"""
            else:
                # 全屏模式
                scale_info = f"""重要：图片上的红色网格数字直接代表屏幕的真实像素坐标。
请直接读取网格上的数值作为坐标，无需任何缩放或转换计算。
例如：如果网格显示"500"，则坐标就是500（真实屏幕像素）。
坐标范围：x=0到{original_width}, y=0到{original_height}。请使用格式"坐标: x, y"。"""
        else:
            scale_info = ""
        
        enhanced_prompt = f"{prompt_text}\n\n{scale_info}"

        payload = {
            "model": DASHSCOPE_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": enhanced_prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_content
                            }
                        }
                    ]
                }
            ]
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=DASHSCOPE_API_TIMEOUT, proxies={"http": None, "https": None})
        if response.status_code == 200:
            res = response.json()
            try:
                description = res['choices'][0]['message']['content']
                prefix = "屏幕区域描述" if only_mouse_area else "屏幕描述"
                if not simple_recognition: prefix += "(含鼠标)"
                
                result = f"{prefix}: {description}"
                # 附加缩放和裁剪信息供 smart_automation_tool 解析
                scale_info_str = f"[SCALE_INFO:original={original_width}x{original_height},thumbnail={screenshot.width}x{screenshot.height},scale_x={scale_x:.4f},scale_y={scale_y:.4f}"
                if crop_offset_x != 0 or crop_offset_y != 0:
                    scale_info_str += f",crop_offset_x={crop_offset_x},crop_offset_y={crop_offset_y}"
                scale_info_str += "]"
                result += f"\n{scale_info_str}"
                
                return result
            except (KeyError, IndexError, TypeError):
                 return "VLM response parsing error."
        else:
            return f"Error: DashScope returned status {response.status_code}"
            
    except Exception as e:
        return f"Error capturing screen: {e}"
    finally:
        # 释放锁和状态标志
        _screen_analysis_in_progress = False
        _screen_analysis_lock.release()

def _add_grid_overlay(image, step=100, font_scale=1.0, crop_offset_x=0, crop_offset_y=0):
    """
    在图片上绘制坐标网格以辅助VLM定位
    网格数字直接代表真实屏幕坐标（而非缩略图坐标）
    
    Args:
        image: PIL Image 对象
        step: 网格间距（像素）
        font_scale: 字体缩放比例，用于在图片缩小后保持字体清晰可读
        crop_offset_x: 裁剪区域的X偏移量（用于在裁剪模式下显示真实屏幕坐标）
        crop_offset_y: 裁剪区域的Y偏移量（用于在裁剪模式下显示真实屏幕坐标）
    """
    from PIL import ImageDraw, ImageFont
    
    draw = ImageDraw.Draw(image)
    width, height = image.size
    
    # 根据 font_scale 动态调整字体大小
    # 基础字体大小，乘以缩放比例确保缩小后依然清晰
    base_font_size = 16
    font_size = int(base_font_size * font_scale)
    # 限制字体大小范围，避免过大或过小
    font_size = max(12, min(font_size, 48))
    
    try:
        try:
            # 尝试使用系统字体，字体大小根据缩放比例调整
            font = ImageFont.truetype("arial.ttf", font_size)
        except IOError:
            try:
                # 尝试其他常见字体
                font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", font_size)
            except IOError:
                font = ImageFont.load_default()
    except IOError:
        font = None

    # 半透明红色网格
    grid_color = (255, 0, 0, 128)
    text_color = (255, 0, 0)
    white_color = (255, 255, 255)
    
    # 根据字体大小调整线条宽度，确保缩小后依然可见
    line_width = max(1, int(2 * font_scale / 3))  # 线条宽度也随缩放调整
    
    # 【修复】计算网格线的起始位置，确保网格数字代表真实屏幕坐标
    # 如果图片被裁剪了，需要调整网格线的起始位置，使网格数字从裁剪偏移量开始
    # 计算第一个网格线的真实屏幕坐标（向上取整到下一个step的倍数）
    if crop_offset_x % step == 0:
        # 如果裁剪偏移量正好是step的倍数，第一个网格线就是偏移量本身
        first_grid_x = crop_offset_x
        start_x = 0
    else:
        # 否则，向上取整到下一个step的倍数
        first_grid_x = ((crop_offset_x // step) + 1) * step
        start_x = first_grid_x - crop_offset_x
    
    if crop_offset_y % step == 0:
        # 如果裁剪偏移量正好是step的倍数，第一个网格线就是偏移量本身
        first_grid_y = crop_offset_y
        start_y = 0
    else:
        # 否则，向上取整到下一个step的倍数
        first_grid_y = ((crop_offset_y // step) + 1) * step
        start_y = first_grid_y - crop_offset_y
    
    # 【修复】确保起始位置不为负数（理论上不应该发生，但作为安全检查）
    if start_x < 0:
        start_x = 0
        first_grid_x = crop_offset_x
    if start_y < 0:
        start_y = 0
        first_grid_y = crop_offset_y
    
    # 垂直线
    grid_x = first_grid_x
    x = start_x
    while x < width:
        draw.line([(x, 0), (x, height)], fill=grid_color, width=line_width)
        if font:
            # 【修复】显示真实屏幕坐标，而不是图片内的相对坐标
            text = str(grid_x)
            # 增加文字偏移量，避免与网格线重叠
            offset = max(3, int(5 * font_scale / 3))
            text_x = x + offset
            text_y = offset
            # 【修复】检查文字位置是否在图片范围内，避免超出边界
            if text_x < width - 20 and text_y < height - 10:  # 留出一些边距
                draw.text((text_x, text_y), text, fill=white_color, font=font)
                draw.text((text_x - 1, text_y - 1), text, fill=text_color, font=font)
        x += step
        grid_x += step
        
    # 水平线
    grid_y = first_grid_y
    y = start_y
    while y < height:
        draw.line([(0, y), (width, y)], fill=grid_color, width=line_width)
        if font:
            # 【修复】显示真实屏幕坐标，而不是图片内的相对坐标
            text = str(grid_y)
            # 增加文字偏移量，避免与网格线重叠
            offset = max(3, int(5 * font_scale / 3))
            text_x = offset
            text_y = y + offset
            # 【修复】检查文字位置是否在图片范围内，避免超出边界
            if text_x < width - 20 and text_y < height - 10:  # 留出一些边距
                draw.text((text_x, text_y), text, fill=white_color, font=font)
                draw.text((text_x - 1, text_y - 1), text, fill=text_color, font=font)
        y += step
        grid_y += step
            
    return image

def get_screen_info_wrapper(**kwargs):
    """
    包装 get_screen_info 以支持自主选择是否输出
    
    [迁移说明] 此函数从 handle_zmq.py 迁移而来，用于保持接口一致性。
    当前实现直接调用原始函数，未来可以在此添加额外的处理逻辑。
    """
    # 调用原始函数获取屏幕信息
    screen_info = get_screen_info(**kwargs)
    
    # 【新增】标记这是屏幕分析结果，供后续处理判断是否输出
    # 在返回结果中添加标记，表示这是屏幕分析信息
    # 实际过滤逻辑在消息构建时处理
    return screen_info

if __name__ == "__main__":
    print(get_screen_info(simple_recognition=False))
    print(get_screen_info(simple_recognition=False))