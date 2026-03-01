"""
智能自动化工具 - 结合屏幕分析和自动化操作
通过屏幕分析识别UI元素位置，然后执行自动化操作
"""
import time
import base64
import io
import requests
from .screen_tool import get_screen_info
# 延迟导入 activate_window 以避免循环引用
# activate_window 将在函数内部按需导入


def find_element_and_click(element_description, target_window=None, search_area=None, retry_times=2):
    """
    通过屏幕分析找到指定元素并点击
    
    Args:
        element_description (str): 要查找的元素描述，例如："搜索按钮"、"确定按钮"、"关闭按钮"等
        target_window (str, optional): 目标窗口标题，用于先激活窗口
        search_area (str, optional): 搜索区域，格式 "x,y,width,height"，如果提供则只在该区域搜索
        retry_times (int): 重试次数，默认2次
    
    Returns:
        str: 操作结果描述
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
    except ImportError:
        return "Error: Missing dependency 'pyautogui'. Please install it: pip install pyautogui"
    
    # 如果指定了窗口，先激活（延迟导入以避免循环引用）
    if target_window:
        from .automation_tool import activate_window
        if not activate_window(target_window):
            return f"Error: 无法激活窗口 '{target_window}'"
        time.sleep(0.5)
    
    # 构建搜索提示词 - 要求 VLM 返回 0~1000 归一化坐标，由本端转换为像素
    focus_prompt = f"请仔细分析屏幕，找到'{element_description}'的位置。请返回该元素中心点的归一化坐标：x、y 均为 0 到 1000 的整数，表示相对屏幕宽度/高度的比例（500,500 表示屏幕中心）。严格按以下格式返回：归一化坐标: x,y （例如：归一化坐标: 500,300）。如果找不到该元素，请返回：未找到。"
    
    if search_area:
        focus_prompt += f" 注意：搜索区域限制在 {search_area}。"
    
    # 使用屏幕分析查找元素
    for attempt in range(retry_times + 1):
        try:
            # 【修复】search_area 和 only_mouse_area 是不同概念
            # search_area: 指定 VLM 关注的区域（通过 focus_description 传递）
            # only_mouse_area: 裁剪鼠标周围区域（会改变截图范围）
            # 如果指定了 search_area，应该在全屏截图中通过提示词限制搜索范围，而不是裁剪
            screen_result = get_screen_info(
                simple_recognition=False,
                only_mouse_area=False,  # 【修复】search_area 不应触发 only_mouse_area
                focus_description=focus_prompt,  # focus_prompt 中已包含 search_area 信息
                fast_mode=True
            )
            
            # 从结果中提取坐标（支持归一化坐标，内部已转为像素）
            coordinates = _extract_coordinates_from_description(screen_result, element_description)
            
            if coordinates:
                x, y = coordinates  # 已为像素坐标
                screen_width, screen_height = pyautogui.size()
                if x < 0 or x >= screen_width or y < 0 or y >= screen_height:
                    print(f"[Smart Automation] 错误: 坐标({x}, {y})超出主显示屏范围({screen_width}x{screen_height})")
                    if attempt < retry_times:
                        time.sleep(0.5)
                        continue
                    else:
                        return f"错误: 坐标({x}, {y})超出主显示屏范围，无法执行操作"
                
                # 【修复】先移动鼠标到目标位置，确保焦点在主显示屏
                current_x, current_y = pyautogui.position()
                # 如果当前鼠标不在主显示屏，先移动到主显示屏中心
                if current_x < 0 or current_x >= screen_width or current_y < 0 or current_y >= screen_height:
                    print(f"[Smart Automation] 检测到鼠标在其他显示屏，先移动到主显示屏中心")
                    pyautogui.moveTo(screen_width // 2, screen_height // 2)
                    time.sleep(0.2)
                
                # 移动鼠标到目标位置并点击（确保焦点正确）
                print(f"[Smart Automation] 找到元素 '{element_description}' 在坐标 ({x}, {y})，准备点击...")
                pyautogui.moveTo(x, y, duration=0.1)
                time.sleep(0.1)
                # 【修复】使用当前位置点击，避免重复移动
                pyautogui.click()
                time.sleep(0.3)
                return f"已找到并点击 '{element_description}' (坐标: {x}, {y})"
            else:
                print(f"[Smart Automation] 第 {attempt + 1} 次尝试未找到元素 '{element_description}'")
                if attempt < retry_times:
                    time.sleep(0.5)  # 等待后重试
                    continue
                else:
                    return f"无法找到元素 '{element_description}'。屏幕分析结果: {screen_result[:200]}"
        
        except Exception as e:
            if attempt < retry_times:
                time.sleep(0.5)
                continue
            else:
                return f"查找元素时出错: {str(e)}"
    
    return f"查找元素 '{element_description}' 失败，已重试 {retry_times + 1} 次"


def find_element_and_type(element_description, text, target_window=None, search_area=None):
    """
    通过屏幕分析找到输入框并输入文本
    
    Args:
        element_description (str): 要查找的输入框描述，例如："搜索框"、"用户名输入框"等
        text (str): 要输入的文本
        target_window (str, optional): 目标窗口标题
        search_area (str, optional): 搜索区域，格式 "x,y,width,height"
    
    Returns:
        str: 操作结果描述
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
    except ImportError:
        return "Error: Missing dependency 'pyautogui'. Please install it: pip install pyautogui"
    
    # 如果指定了窗口，先激活（延迟导入以避免循环引用）
    if target_window:
        from .automation_tool import activate_window
        if not activate_window(target_window):
            return f"Error: 无法激活窗口 '{target_window}'"
        time.sleep(0.5)
    
    # 构建搜索提示词 - 要求 VLM 返回 0~1000 归一化坐标，由本端转换为像素
    focus_prompt = f"请仔细分析屏幕，找到'{element_description}'的位置。请返回该元素中心点的归一化坐标：x、y 均为 0 到 1000 的整数（500,500 表示屏幕中心）。严格按以下格式返回：归一化坐标: x,y （例如：归一化坐标: 500,300）。如果找不到该元素，请返回：未找到。"
    
    if search_area:
        focus_prompt += f" 注意：搜索区域限制在 {search_area}。"
    
    try:
        # 【修复】search_area 和 only_mouse_area 是不同概念
        # search_area: 指定 VLM 关注的区域（通过 focus_description 传递）
        # only_mouse_area: 裁剪鼠标周围区域（会改变截图范围）
        # 如果指定了 search_area，应该在全屏截图中通过提示词限制搜索范围，而不是裁剪
        screen_result = get_screen_info(
            simple_recognition=False,
            only_mouse_area=False,  # 【修复】search_area 不应触发 only_mouse_area
            focus_description=focus_prompt,  # focus_prompt 中已包含 search_area 信息
            fast_mode=True
        )
        
        # 从结果中提取坐标
        coordinates = _extract_coordinates_from_description(screen_result, element_description)
        
        if coordinates:
            x, y = coordinates  # 已为像素坐标
            print(f"[Smart Automation] 找到输入框 '{element_description}' 在坐标 ({x}, {y})，准备输入文本...")
            
            screen_width, screen_height = pyautogui.size()
            if x < 0 or x >= screen_width or y < 0 or y >= screen_height:
                print(f"[Smart Automation] 错误: 坐标({x}, {y})超出主显示屏范围({screen_width}x{screen_height})")
                return f"错误: 坐标({x}, {y})超出主显示屏范围，无法执行操作"
            
            # 【修复】先移动鼠标到目标位置，确保焦点在主显示屏
            current_x, current_y = pyautogui.position()
            # 如果当前鼠标不在主显示屏，先移动到主显示屏中心
            if current_x < 0 or current_x >= screen_width or current_y < 0 or current_y >= screen_height:
                print(f"[Smart Automation] 检测到鼠标在其他显示屏，先移动到主显示屏中心")
                pyautogui.moveTo(screen_width // 2, screen_height // 2)
                time.sleep(0.2)
            
            # 移动鼠标到目标位置并点击（确保焦点正确）
            pyautogui.moveTo(x, y, duration=0.1)
            time.sleep(0.1)
            # 【修复】使用当前位置点击，避免重复移动
            pyautogui.click()
            time.sleep(0.3)
            
            # 清空并输入文本
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.1)
            pyautogui.press('delete')
            time.sleep(0.1)
            pyautogui.write(text, interval=0.05)
            time.sleep(0.2)
            return f"已在 '{element_description}' (坐标: {x}, {y}) 输入文本: {text}"
        else:
            return f"无法找到输入框 '{element_description}'。屏幕分析结果: {screen_result[:200]}"
    
    except Exception as e:
        return f"查找输入框时出错: {str(e)}"


def analyze_and_operate(operation_description, target_window=None):
    """
    分析屏幕并执行操作（通用接口）
    
    Args:
        operation_description (str): 操作描述，例如："点击确定按钮"、"在搜索框中输入'hello'"、"找到关闭按钮并点击"
        target_window (str, optional): 目标窗口标题
    
    Returns:
        str: 操作结果描述
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
    except ImportError:
        return "Error: Missing dependency 'pyautogui'. Please install it: pip install pyautogui"
    
    # 如果指定了窗口，先激活（延迟导入以避免循环引用）
    if target_window:
        from .automation_tool import activate_window
        if not activate_window(target_window):
            return f"Error: 无法激活窗口 '{target_window}'"
        time.sleep(0.5)
    
    # 构建分析提示词 - 要求 VLM 返回 0~1000 归一化坐标和操作信息，由本端转为像素
    focus_prompt = f"用户要求：{operation_description}。请仔细分析屏幕，找到需要操作的元素。请返回该元素中心点的归一化坐标：x、y 均为 0 到 1000 的整数（500,500 表示屏幕中心）。严格按以下格式返回：归一化坐标: x,y，操作: [点击/输入]，文本: [如需输入则填内容]。例如：归一化坐标: 500,300，操作: 点击。或：归一化坐标: 400,200，操作: 输入，文本: hello。"
    
    try:
        # 调用屏幕分析工具
        screen_result = get_screen_info(
            simple_recognition=False,
            only_mouse_area=False,
            focus_description=focus_prompt,
            fast_mode=False  # 使用完整分析以获得更准确的结果
        )
        
        # 从结果中提取坐标和操作类型
        result = _parse_operation_from_description(screen_result, operation_description)
        
        if result:
            if result['action'] == 'click':
                x, y = result['coordinates']
                # 【修复】验证坐标并先移动鼠标
                screen_width, screen_height = pyautogui.size()
                if x < 0 or x >= screen_width or y < 0 or y >= screen_height:
                    return f"错误: 坐标 ({x}, {y}) 超出屏幕范围 ({screen_width}x{screen_height})"
                # 先移动鼠标到目标位置
                pyautogui.moveTo(x, y, duration=0.1)
                time.sleep(0.1)
                pyautogui.click()
                time.sleep(0.3)
                return f"已执行点击操作 (坐标: {x}, {y})"
            elif result['action'] == 'type':
                x, y = result['coordinates']
                text = result.get('text', '')
                # 【修复】验证坐标并先移动鼠标
                screen_width, screen_height = pyautogui.size()
                if x < 0 or x >= screen_width or y < 0 or y >= screen_height:
                    return f"错误: 坐标 ({x}, {y}) 超出屏幕范围 ({screen_width}x{screen_height})"
                # 先移动鼠标到目标位置
                pyautogui.moveTo(x, y, duration=0.1)
                time.sleep(0.1)
                pyautogui.click()
                time.sleep(0.3)
                pyautogui.hotkey('ctrl', 'a')
                time.sleep(0.1)
                pyautogui.press('delete')
                time.sleep(0.1)
                pyautogui.write(text, interval=0.05)
                time.sleep(0.2)
                return f"已在坐标 ({x}, {y}) 输入文本: {text}"
            else:
                return f"解析到操作类型: {result['action']}，但未实现"
        else:
            return f"无法从屏幕分析中解析操作。分析结果: {screen_result[:300]}"
    
    except Exception as e:
        return f"分析并操作时出错: {str(e)}"


# 归一化坐标范围：VLM 返回 0~1000，对应屏幕宽高的比例
NORM_COORD_MAX = 1000


def _extract_coordinates_from_description(description, element_name):
    """
    从屏幕分析描述中提取坐标，并转换为实际屏幕像素坐标。
    优先解析 VLM 返回的 0~1000 归一化坐标：pixel_x = (norm_x/1000)*screen_width，再裁剪到屏幕内。
    
    Args:
        description (str): 屏幕分析结果
        element_name (str): 元素名称（用于日志）
    
    Returns:
        tuple: (x, y) 实际屏幕像素坐标，如果找不到返回 None
    """
    import re
    
    try:
        import pyautogui
        screen_width, screen_height = pyautogui.size()
    except Exception:
        screen_width, screen_height = 1920, 1080
    
    # 1）优先解析 0~1000 归一化坐标
    norm_patterns = [
        r'归一化坐标[：:]\s*([\d.]+)\s*[,，]\s*([\d.]+)',
        r'归一化[：:]\s*([\d.]+)\s*[,，]\s*([\d.]+)',
        r'normalized[：:]\s*([\d.]+)\s*[,，]\s*([\d.]+)',
    ]
    for pattern in norm_patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            try:
                nx = float(match.group(1))
                ny = float(match.group(2))
                # 限制在 [0, NORM_COORD_MAX]
                nx = max(0.0, min(float(NORM_COORD_MAX), nx))
                ny = max(0.0, min(float(NORM_COORD_MAX), ny))
                # 换算到像素：norm 在 [0,1000] -> 像素在 [0, screen_width] / [0, screen_height]
                pixel_x = int(round((nx / NORM_COORD_MAX) * screen_width))
                pixel_y = int(round((ny / NORM_COORD_MAX) * screen_height))
                # 裁剪到有效像素范围 [0, width-1], [0, height-1]
                pixel_x = max(0, min(pixel_x, screen_width - 1))
                pixel_y = max(0, min(pixel_y, screen_height - 1))
                print(f"[Smart Automation] 归一化(0~1000) ({int(nx)},{int(ny)}) -> 像素 ({pixel_x},{pixel_y}) [屏幕 {screen_width}x{screen_height}]", flush=True)
                return (pixel_x, pixel_y)
            except (ValueError, IndexError):
                continue
    
    # 2）兼容旧格式：像素坐标 "坐标: 500,300"
    pixel_patterns = [
        r'坐标[：:]\s*(\d+)\s*[,，]\s*(\d+)',
        r'[\(（]\s*(\d+)\s*[,，]\s*(\d+)\s*[\)）]',
    ]
    for pattern in pixel_patterns:
        match = re.search(pattern, description)
        if match:
            try:
                px = int(match.group(1))
                py = int(match.group(2))
                if 0 <= px < screen_width * 2 and 0 <= py < screen_height * 2:
                    px = max(0, min(px, screen_width - 1))
                    py = max(0, min(py, screen_height - 1))
                    print(f"[Smart Automation] 从描述中解析到像素坐标 ({px}, {py})（已裁剪到屏幕内）", flush=True)
                    return (px, py)
            except (ValueError, IndexError):
                continue
    
    return None


def _parse_operation_from_description(description, operation_description):
    """
    从操作描述和屏幕分析结果中解析操作
    
    Args:
        description (str): 屏幕分析结果
        operation_description (str): 原始操作描述
    
    Returns:
        dict: 包含 action, coordinates, text 等信息的字典
    """
    import re
    
    # 判断操作类型
    if '输入' in operation_description or 'type' in operation_description.lower():
        action = 'type'
        # 尝试提取要输入的文本
        text_match = re.search(r'输入["\']?([^"\']+)["\']?', operation_description)
        text = text_match.group(1) if text_match else ''
    elif '点击' in operation_description or 'click' in operation_description.lower():
        action = 'click'
        text = ''
    else:
        action = 'click'  # 默认点击
        text = ''
    
    # 提取坐标
    coordinates = _extract_coordinates_from_description(description, '')
    
    if coordinates:
        return {
            'action': action,
            'coordinates': coordinates,
            'text': text
        }
    
    return None


def smart_click(element_description, target_window=None, search_area=None):
    """
    智能点击 - 通过屏幕分析找到元素并点击（简化接口）
    
    Args:
        element_description (str): 要查找的元素描述
        target_window (str, optional): 目标窗口标题
        search_area (str, optional): 搜索区域，格式 "x,y,width,height"
    
    Returns:
        str: 操作结果描述
    """
    return find_element_and_click(element_description, target_window, search_area)


def smart_type(element_description, text, target_window=None, search_area=None):
    """
    智能输入 - 通过屏幕分析找到输入框并输入文本（简化接口）
    
    Args:
        element_description (str): 要查找的输入框描述
        text (str): 要输入的文本
        target_window (str, optional): 目标窗口标题
        search_area (str, optional): 搜索区域，格式 "x,y,width,height"
    
    Returns:
        str: 操作结果描述
    """
    return find_element_and_type(element_description, text, target_window, search_area)


if __name__ == "__main__":
    # 测试
    print("测试智能点击...")
    result = smart_click("确定按钮", target_window="记事本")
    print(result)
    
    print("\n测试智能输入...")
    result = smart_type("搜索框", "hello world", target_window="浏览器")
    print(result)
