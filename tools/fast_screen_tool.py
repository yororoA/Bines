"""
快速屏幕识别工具 - 专为游戏和实时操作优化
使用本地OCR和轻量级图像识别，避免API延迟
"""
import io
import base64
import time

def fast_screen_analysis(focus_area=None, use_ocr=True):
    """
    快速屏幕分析 - 专为实时操作优化
    
    Args:
        focus_area (tuple, optional): 关注区域 (x, y, width, height)，如果提供则只分析该区域
        use_ocr (bool): 是否使用OCR识别文字（更快但可能不够准确）
    
    Returns:
        dict: 包含屏幕信息的字典
    """
    try:
        import pyautogui
        from PIL import Image, ImageDraw
    except ImportError:
        return {"error": "Missing dependencies: pyautogui, pillow"}
    
    try:
        start_time = time.time()
        
        # 1. 快速截图
        screenshot = pyautogui.screenshot()
        
        # 2. 如果指定了关注区域，裁剪
        if focus_area:
            x, y, w, h = focus_area
            screenshot = screenshot.crop((x, y, x + w, y + h))
        
        # 3. 快速压缩（降低分辨率以加快处理）
        screenshot.thumbnail((640, 480))  # 游戏场景通常不需要全分辨率
        
        # 4. 尝试使用OCR快速识别文字（如果可用）
        text_content = ""
        if use_ocr:
            try:
                import pytesseract
                # 使用OCR快速识别文字
                text_content = pytesseract.image_to_string(screenshot, lang='chi_sim+eng', config='--psm 6')
                text_content = text_content.strip()
            except ImportError:
                # OCR不可用，跳过
                pass
            except Exception as e:
                print(f"[Fast Screen] OCR error: {e}")
        
        # 5. 快速颜色分析（检测特定颜色区域，用于游戏UI识别）
        color_info = analyze_colors(screenshot)
        
        # 6. 转换为base64（如果需要）
        buff = io.BytesIO()
        screenshot.save(buff, format="JPEG", quality=60, optimize=True)
        img_b64 = base64.b64encode(buff.getvalue()).decode('utf-8')
        
        elapsed = time.time() - start_time
        
        return {
            "text": text_content,
            "colors": color_info,
            "image_b64": img_b64,
            "size": screenshot.size,
            "processing_time": elapsed
        }
    
    except Exception as e:
        return {"error": f"Fast screen analysis failed: {e}"}


def analyze_colors(image, sample_points=100):
    """
    快速分析图像中的主要颜色
    用于快速识别游戏UI元素（血条、能量条等）
    """
    try:
        import numpy as np
        from collections import Counter
        
        # 转换为numpy数组
        img_array = np.array(image)
        
        # 采样点（避免分析整个图像）
        h, w = img_array.shape[:2]
        step = max(1, min(h, w) // int(sample_points ** 0.5))
        
        colors = []
        for y in range(0, h, step):
            for x in range(0, w, step):
                pixel = img_array[y, x]
                if len(pixel) == 3:  # RGB
                    colors.append(tuple(pixel))
        
        # 统计主要颜色
        color_counter = Counter(colors)
        top_colors = color_counter.most_common(5)
        
        return {
            "dominant_colors": [{"rgb": list(c[0]), "count": c[1]} for c in top_colors]
        }
    except ImportError:
        return {"error": "numpy not available"}
    except Exception as e:
        return {"error": str(e)}


def find_color_region_in_screen(target_color, tolerance=30, focus_area=None):
    """
    在屏幕中查找特定颜色区域（用于游戏UI元素定位）
    
    Args:
        target_color: (R, G, B) 目标颜色或字符串 "R,G,B"
        tolerance: 颜色容差
        focus_area: (x, y, width, height) 搜索区域或字符串 "x,y,width,height"
    
    Returns:
        dict: 找到的区域信息
    """
    try:
        import pyautogui
        from PIL import Image
        
        # 截图
        screenshot = pyautogui.screenshot()
        
        # 解析关注区域（如果提供）
        crop_offset = None
        if focus_area:
            if isinstance(focus_area, str):
                x, y, w, h = map(int, focus_area.split(','))
            else:
                x, y, w, h = focus_area
            screenshot = screenshot.crop((x, y, x + w, y + h))
            crop_offset = (x, y)  # 保存偏移量，用于后续坐标调整
        
        # 解析目标颜色
        if isinstance(target_color, str):
            r, g, b = map(int, target_color.split(','))
        else:
            r, g, b = target_color
        
        result = find_color_region(screenshot, (r, g, b), tolerance)
        
        # 如果指定了关注区域，需要调整坐标
        if crop_offset and result.get("found"):
            x, y = crop_offset
            result["center"] = (result["center"][0] + x, result["center"][1] + y)
            result["bounds"]["x"] += x
            result["bounds"]["y"] += y
        
        return result
    except Exception as e:
        return {"error": str(e)}


def find_color_region(image, target_color, tolerance=30):
    """
    在图像中查找特定颜色区域（用于游戏UI元素定位）
    
    Args:
        image: PIL Image
        target_color: (R, G, B) 目标颜色
        tolerance: 颜色容差
    
    Returns:
        dict: 找到的区域信息
    """
    try:
        import numpy as np
        
        img_array = np.array(image)
        target = np.array(target_color)
        
        # 计算颜色距离
        diff = np.abs(img_array - target)
        mask = np.all(diff <= tolerance, axis=2)
        
        # 找到所有匹配的像素
        y_coords, x_coords = np.where(mask)
        
        if len(x_coords) > 0:
            return {
                "found": True,
                "center": (int(np.mean(x_coords)), int(np.mean(y_coords))),
                "bounds": {
                    "x": int(np.min(x_coords)),
                    "y": int(np.min(y_coords)),
                    "width": int(np.max(x_coords) - np.min(x_coords)),
                    "height": int(np.max(y_coords) - np.min(y_coords))
                },
                "pixel_count": len(x_coords)
            }
        return {"found": False}
    except Exception as e:
        return {"error": str(e)}


def template_match_in_screen(template_path, threshold=0.8, focus_area=None):
    """
    在屏幕中进行模板匹配
    
    Args:
        template_path: 模板图片路径
        threshold: 匹配阈值
        focus_area: (x, y, width, height) 搜索区域
    
    Returns:
        dict: 匹配结果
    """
    try:
        import pyautogui
        from PIL import Image
        
        # 截图
        screenshot = pyautogui.screenshot()
        
        # 解析关注区域（如果提供）
        crop_offset = None
        if focus_area:
            if isinstance(focus_area, str):
                x, y, w, h = map(int, focus_area.split(','))
            else:
                x, y, w, h = focus_area
            screenshot = screenshot.crop((x, y, x + w, y + h))
            crop_offset = (x, y)  # 保存偏移量，用于后续坐标调整
        
        result = template_match(screenshot, template_path, threshold)
        
        # 如果指定了关注区域，需要调整坐标
        if crop_offset and result.get("found"):
            x, y = crop_offset
            result["location"]["x"] += x
            result["location"]["y"] += y
            result["center"] = (result["center"][0] + x, result["center"][1] + y)
        
        return result
    except Exception as e:
        return {"error": str(e)}


def template_match(screenshot, template_path, threshold=0.8):
    """
    模板匹配 - 用于快速识别游戏中的固定UI元素（按钮、图标等）
    
    Args:
        screenshot: PIL Image 或截图
        template_path: 模板图片路径
        threshold: 匹配阈值
    
    Returns:
        dict: 匹配结果
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image
        
        # 转换为OpenCV格式
        if isinstance(screenshot, Image.Image):
            screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        else:
            screenshot_cv = screenshot
        
        # 加载模板
        template = cv2.imread(template_path)
        if template is None:
            return {"error": f"Template not found: {template_path}"}
        
        # 模板匹配
        result = cv2.matchTemplate(screenshot_cv, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
        
        if max_val >= threshold:
            h, w = template.shape[:2]
            return {
                "found": True,
                "confidence": float(max_val),
                "location": {
                    "x": int(max_loc[0]),
                    "y": int(max_loc[1]),
                    "width": w,
                    "height": h
                },
                "center": (int(max_loc[0] + w/2), int(max_loc[1] + h/2))
            }
        return {"found": False, "confidence": float(max_val)}
    except ImportError:
        return {"error": "OpenCV not available"}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    # 测试
    result = fast_screen_analysis()
    print(f"Fast analysis result: {result}")
