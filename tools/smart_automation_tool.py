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
    
    # 构建搜索提示词 - 要求VLM返回精确坐标
    # 注意：网格数字直接代表真实屏幕坐标，无需任何转换
    focus_prompt = f"请仔细分析屏幕，找到'{element_description}'的精确位置。图片上的红色网格数字直接代表屏幕的真实像素坐标，请直接读取网格上的数值。请以以下格式返回坐标：坐标: x,y （例如：坐标: 500,300）。如果找不到该元素，请返回：未找到。"
    
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
            
            # 从结果中提取坐标
            coordinates = _extract_coordinates_from_description(screen_result, element_description)
            
            if coordinates:
                x, y = coordinates
                # 【修复】确保坐标在主显示屏范围内
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
    
    # 构建搜索提示词 - 要求VLM返回精确坐标
    # 注意：网格数字直接代表真实屏幕坐标，无需任何转换
    focus_prompt = f"请仔细分析屏幕，找到'{element_description}'的精确位置。图片上的红色网格数字直接代表屏幕的真实像素坐标，请直接读取网格上的数值。请以以下格式返回坐标：坐标: x,y （例如：坐标: 500,300）。如果找不到该元素，请返回：未找到。"
    
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
            x, y = coordinates
            print(f"[Smart Automation] 找到输入框 '{element_description}' 在坐标 ({x}, {y})，准备输入文本...")
            
            # 【修复】确保坐标在主显示屏范围内
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
    
    # 构建分析提示词 - 要求VLM返回精确坐标和操作信息
    # 注意：网格数字直接代表真实屏幕坐标，无需任何转换
    focus_prompt = f"用户要求：{operation_description}。请仔细分析屏幕，找到需要操作的元素。图片上的红色网格数字直接代表屏幕的真实像素坐标，请直接读取网格上的数值。请以以下格式返回：坐标: x,y，操作: [点击/输入]，文本: [如果需要输入]。例如：坐标: 500,300，操作: 点击。或：坐标: 400,200，操作: 输入，文本: hello。"
    
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


def _extract_coordinates_from_description(description, element_name):
    """
    从屏幕分析描述中提取坐标，并转换为实际屏幕坐标
    
    Args:
        description (str): 屏幕分析结果（可能包含缩放信息）
        element_name (str): 元素名称
    
    Returns:
        tuple: (x, y) 实际屏幕坐标，如果找不到返回 None
    """
    import re
    
    # 首先提取缩放信息和裁剪偏移量
    scale_info = {}
    # 匹配可能包含裁剪偏移量的缩放信息
    scale_match = re.search(r'\[SCALE_INFO:original=(\d+)x(\d+),thumbnail=(\d+)x(\d+),scale_x=([\d.]+),scale_y=([\d.]+)(?:,crop_offset_x=(\d+),crop_offset_y=(\d+))?\]', description)
    if scale_match:
        scale_info = {
            'original_width': int(scale_match.group(1)),
            'original_height': int(scale_match.group(2)),
            'thumbnail_width': int(scale_match.group(3)),
            'thumbnail_height': int(scale_match.group(4)),
            'scale_x': float(scale_match.group(5)),
            'scale_y': float(scale_match.group(6)),
            'crop_offset_x': int(scale_match.group(7)) if scale_match.group(7) else 0,
            'crop_offset_y': int(scale_match.group(8)) if scale_match.group(8) else 0
        }
        print(f"[Smart Automation] 检测到缩放信息: 原始={scale_info['original_width']}x{scale_info['original_height']}, "
              f"缩略图={scale_info['thumbnail_width']}x{scale_info['thumbnail_height']}, "
              f"缩放比例={scale_info['scale_x']:.2f}x{scale_info['scale_y']:.2f}")
        if scale_info['crop_offset_x'] != 0 or scale_info['crop_offset_y'] != 0:
            print(f"[Smart Automation] 检测到裁剪偏移: ({scale_info['crop_offset_x']}, {scale_info['crop_offset_y']})")
    
    # 【修复】添加验证函数，确保提取的坐标在合理范围内
    def is_valid_coordinate(x, y, max_x=10000, max_y=10000):
        """验证坐标是否在合理范围内（避免误匹配分辨率等大数字）"""
        try:
            x_int = int(x)
            y_int = int(y)
            # 坐标应该在屏幕范围内（允许一些余量）
            return 0 <= x_int < max_x and 0 <= y_int < max_y
        except:
            return False
    
    # 【修复】尝试多种格式提取坐标，按优先级排序，避免误匹配
    # 更严格的正则表达式，优先匹配明确的坐标格式，避免误匹配分辨率等数字
    patterns = [
        r'坐标[：:]\s*(\d+)\s*[,，]\s*(\d+)',     # 最严格：坐标: 100, 200
        r'坐标[：:]\s*(\d+)\s+[:]\s+(\d+)',       # 容错
        r'[\(（]\s*(\d+)\s*[,，]\s*(\d+)\s*[\)）]', # (100, 200)
        r'point\s*[:]\s*(\d+)\s*[,，]\s*(\d+)',    # 英文容错
        # 移除过于宽泛的匹配，防止匹配到日期或分辨率
    ]
    
    thumbnail_x = None
    thumbnail_y = None
    
    # 【修复】获取屏幕尺寸用于验证坐标合理性
    try:
        import pyautogui
        screen_width, screen_height = pyautogui.size()
        max_coord_x = screen_width * 2  # 允许一些余量（多屏环境）
        max_coord_y = screen_height * 2
    except:
        max_coord_x = 10000
        max_coord_y = 10000
    
    for pattern in patterns:
        matches = re.findall(pattern, description)
        if matches:
            try:
                candidate_x = int(matches[0][0])
                candidate_y = int(matches[0][1])
                # 【修复】验证坐标是否在合理范围内，避免误匹配分辨率等
                if is_valid_coordinate(candidate_x, candidate_y, max_coord_x, max_coord_y):
                    thumbnail_x = candidate_x
                    thumbnail_y = candidate_y
                    print(f"[Smart Automation] 从描述中提取到坐标: ({thumbnail_x}, {thumbnail_y})")
                    break
                else:
                    print(f"[Smart Automation] 跳过不合理的坐标: ({candidate_x}, {candidate_y})，可能误匹配")
            except (ValueError, IndexError):
                continue
    
    if thumbnail_x is None or thumbnail_y is None:
        return None
    
    # 【核心优化】网格数字直接代表真实屏幕坐标
    # 由于在绘制网格时已经考虑了裁剪偏移量，网格数字已经是真实屏幕坐标
    # scale_x 和 scale_y 现在都是 1.0，所以无需缩放计算，也无需再加裁剪偏移
    if scale_info:
        # 坐标转换步骤：
        # 1. AI返回的坐标 (thumbnail_x, thumbnail_y) - 这些已经是真实屏幕坐标（网格数字）
        #    注意：即使在裁剪模式下，网格数字也已经包含了裁剪偏移量，所以直接使用即可
        # 2. 由于 scale_x=1.0, scale_y=1.0，无需缩放计算
        # 3. 【修复】由于网格数字已经是真实屏幕坐标（已包含裁剪偏移），无需再加裁剪偏移量
        
        # 步骤1: 由于 scale_x 和 scale_y 都是 1.0，直接使用 AI 返回的坐标
        scaled_x = thumbnail_x * scale_info['scale_x']  # 实际上就是 thumbnail_x * 1.0
        scaled_y = thumbnail_y * scale_info['scale_y']  # 实际上就是 thumbnail_y * 1.0
        
        # 步骤2: 【修复】网格数字已经是真实屏幕坐标（绘制时已考虑裁剪偏移），无需再加裁剪偏移量
        actual_x = int(round(scaled_x))
        actual_y = int(round(scaled_y))
        
        if scale_info.get('crop_offset_x', 0) != 0 or scale_info.get('crop_offset_y', 0) != 0:
            print(f"[Smart Automation] 坐标转换: AI返回坐标({thumbnail_x}, {thumbnail_y}) [已是真实屏幕坐标，网格已包含裁剪偏移({scale_info['crop_offset_x']}, {scale_info['crop_offset_y']})] -> "
                  f"最终屏幕坐标({actual_x}, {actual_y})")
        else:
            print(f"[Smart Automation] 坐标转换: AI返回坐标({thumbnail_x}, {thumbnail_y}) [已是真实坐标，无需转换]")
        
        # 【修复】验证坐标是否在主显示屏范围内（强制限制在主显示屏）
        try:
            import pyautogui
            screen_width, screen_height = pyautogui.size()
            
            # 强制限制在主显示屏范围内（pyautogui.size()返回的是主显示屏尺寸）
            if actual_x < 0 or actual_x >= screen_width or actual_y < 0 or actual_y >= screen_height:
                print(f"[Smart Automation] 警告: 坐标({actual_x}, {actual_y})超出主显示屏范围({screen_width}x{screen_height})，已修正")
                # 限制在主显示屏范围内
                actual_x = max(0, min(actual_x, screen_width - 1))
                actual_y = max(0, min(actual_y, screen_height - 1))
                print(f"[Smart Automation] 坐标已修正为: ({actual_x}, {actual_y})")
            
            return (actual_x, actual_y)
        except:
            return (actual_x, actual_y)
    else:
        # 没有缩放信息，假设返回的就是实际坐标
        print(f"[Smart Automation] 未检测到缩放信息，使用原始坐标: ({thumbnail_x}, {thumbnail_y})")
        try:
            import pyautogui
            screen_width, screen_height = pyautogui.size()
            # 【修复】强制限制在主显示屏范围内
            if thumbnail_x < 0 or thumbnail_x >= screen_width or thumbnail_y < 0 or thumbnail_y >= screen_height:
                print(f"[Smart Automation] 警告: 坐标({thumbnail_x}, {thumbnail_y})超出主显示屏范围({screen_width}x{screen_height})，已修正")
                thumbnail_x = max(0, min(thumbnail_x, screen_width - 1))
                thumbnail_y = max(0, min(thumbnail_y, screen_height - 1))
                print(f"[Smart Automation] 坐标已修正为: ({thumbnail_x}, {thumbnail_y})")
            return (thumbnail_x, thumbnail_y)
        except:
            return (thumbnail_x, thumbnail_y)


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
