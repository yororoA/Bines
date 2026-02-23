import time

# 延迟导入智能自动化工具以避免循环引用
# 这些函数将在需要时按需导入
SMART_AUTOMATION_AVAILABLE = None  # 延迟检查
find_element_and_click = None
find_element_and_type = None
analyze_and_operate = None

def _ensure_smart_automation_loaded():
    """延迟加载智能自动化工具，避免循环引用"""
    global SMART_AUTOMATION_AVAILABLE, find_element_and_click, find_element_and_type, analyze_and_operate
    if SMART_AUTOMATION_AVAILABLE is None:
        try:
            from .smart_automation_tool import find_element_and_click as _feac, find_element_and_type as _feat, analyze_and_operate as _aao
            find_element_and_click = _feac
            find_element_and_type = _feat
            analyze_and_operate = _aao
            SMART_AUTOMATION_AVAILABLE = True
        except ImportError:
            SMART_AUTOMATION_AVAILABLE = False
            find_element_and_click = None
            find_element_and_type = None
            analyze_and_operate = None

def activate_window(target_window):
    """
    激活目标窗口
    
    Args:
        target_window (str): 目标窗口标题
    
    Returns:
        bool: 是否成功激活窗口
    """
    try:
        import pywinauto
        from pywinauto import Application
        from pywinauto.findwindows import find_windows
        
        # 尝试多种方式查找和激活窗口
        # 方式1: 使用find_windows查找窗口
        windows = find_windows(title_re=f".*{target_window}.*", enabled_only=True)
        if windows:
            window = pywinauto.WindowSpecification({'handle': windows[0]})
            if window.exists():
                window.set_focus()
                time.sleep(0.5)  # 等待窗口激活
                return True
        
        # 方式2: 遍历所有顶层窗口
        for w in find_windows(enabled_only=True):
            try:
                window = pywinauto.WindowSpecification({'handle': w})
                if window.exists():
                    title = window.window_text()
                    if target_window.lower() in title.lower():
                        window.set_focus()
                        time.sleep(0.5)
                        return True
            except Exception:
                pass
        
        # 方式3: 尝试使用Application().connect
        try:
            app = Application().connect(title_re=f".*{target_window}.*")
            app.top_window().set_focus()
            time.sleep(0.5)
            return True
        except Exception:
            pass
        
        # 方式4: 尝试Alt+Tab多次
        for _ in range(3):  # 尝试3次
            try:
                import pyautogui
                pyautogui.hotkey('alt', 'tab')
                time.sleep(0.5)
                # 检查当前活动窗口是否是目标窗口
                active_window = pywinauto.Desktop(backend='uia').active_window()
                if active_window and target_window.lower() in active_window.window_text().lower():
                    return True
            except Exception:
                pass
        
    except Exception as e:
        print(f"窗口激活失败: {str(e)}")
    
    return False

def automate_action(action_type, content=None, target_window=None, delay=0.5, coordinates=None):
    """
    自动化操作：在应用程序中执行各种操作
    
    Args:
        action_type (str): 操作类型，支持：
            - "type": 打字输入文本
            - "key": 按键（如 "enter", "ctrl+c", "alt+tab"）
            - "click": 鼠标点击
            - "search": 在浏览器中搜索
            - "draw": 在画图中绘制简单图形
            - "write": 在记事本中写入文本
        content (str, optional): 操作内容，根据 action_type 不同而不同
        target_window (str, optional): 目标窗口标题（用于切换窗口）
        delay (float): 操作之间的延迟时间（秒），默认0.5秒
    
    Returns:
        str: 操作结果描述
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True  # 启用安全模式（鼠标移到屏幕角落会停止）
        # 游戏模式：降低延迟以提高响应速度
        game_mode_delay = 0.01 if delay < 0.1 else delay
        pyautogui.PAUSE = game_mode_delay  # 设置操作之间的延迟
    except ImportError:
        return "Error: Missing dependency 'pyautogui'. Please install it: pip install pyautogui"
    
    try:
        # 如果需要切换窗口
        if target_window:
            print(f"尝试激活窗口: {target_window}")
            if not activate_window(target_window):
                print(f"警告: 无法激活窗口 '{target_window}'，将尝试在当前活动窗口执行操作")
        
        if action_type == "type":
            # 打字输入 - 使用视觉分析定位输入区域
            if not content:
                return "Error: 'type' action requires 'content' parameter"
            
            # 【修复】确保窗口激活，失败则直接返回错误
            if target_window:
                if not activate_window(target_window):
                    return f"Error: 无法激活窗口 '{target_window}'，无法执行输入操作"
            
            # 优先使用视觉分析定位输入区域
            _ensure_smart_automation_loaded()
            if SMART_AUTOMATION_AVAILABLE and find_element_and_type:
                # 尝试查找输入框（如果target_window提供了上下文）
                input_description = "输入框" if not target_window else f"{target_window}中的输入框"
                result = find_element_and_type(
                    element_description=input_description,
                    text=content,
                    target_window=target_window
                )
                if "已找到" in result or "已在" in result:
                    return result
                # 如果视觉分析失败，回退到传统方式
            
            # 回退：使用传统方式（点击编辑区域中心）
            try:
                screen_width, screen_height = pyautogui.size()
                # 点击编辑区域中心（屏幕中央偏上）
                edit_area_x = screen_width // 2
                edit_area_y = screen_height // 2
                pyautogui.click(edit_area_x, edit_area_y)
                time.sleep(0.2)  # 等待焦点获得
            except:
                pass  # 如果点击失败，继续尝试输入
            
            # 确保窗口是活动的
            pyautogui.write(content, interval=0.05)  # 模拟真实打字速度
            return f"已输入文本: {content[:50]}{'...' if len(content) > 50 else ''}"
        
        elif action_type == "key":
            # 按键操作
            if not content:
                return "Error: 'key' action requires 'content' parameter"
            # 支持组合键，如 "ctrl+c", "alt+tab"
            keys = content.lower().split('+')
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
            return f"已执行按键: {content}"
        
        elif action_type == "click":
            # 鼠标点击 - 优先使用视觉分析
            # 如果提供了明确的坐标，直接使用
            if coordinates:
                try:
                    x, y = map(int, coordinates.split(','))
                    # 【修复】验证坐标是否在屏幕范围内
                    screen_width, screen_height = pyautogui.size()
                    if x < 0 or x >= screen_width or y < 0 or y >= screen_height:
                        return f"Error: 坐标 ({x}, {y}) 超出屏幕范围 ({screen_width}x{screen_height})"
                    pyautogui.click(x, y)
                    return f"已在坐标 ({x}, {y}) 点击"
                except (ValueError, IndexError) as e:
                    return f"Error: 'click' action coordinates format error, should be 'x,y': {e}"
            
            if content:
                # 尝试将 content 解析为坐标
                try:
                    x, y = map(int, content.split(','))
                    # 【修复】验证坐标是否在屏幕范围内
                    screen_width, screen_height = pyautogui.size()
                    if x < 0 or x >= screen_width or y < 0 or y >= screen_height:
                        return f"Error: 坐标 ({x}, {y}) 超出屏幕范围 ({screen_width}x{screen_height})"
                    pyautogui.click(x, y)
                    return f"已在坐标 ({x}, {y}) 点击"
                except (ValueError, IndexError):
                    # 如果解析失败，使用视觉分析查找元素
                    _ensure_smart_automation_loaded()
                    if SMART_AUTOMATION_AVAILABLE and find_element_and_click:
                        # content 可能是按钮描述，使用视觉分析查找
                        result = find_element_and_click(
                            element_description=content,
                            target_window=target_window,
                            retry_times=1
                        )
                        if "已找到并点击" in result or "已执行" in result:
                            return result
                        # 如果视觉分析失败，回退到按钮名称方式
                    
                    # 回退：尝试作为按钮名称
                    button_map = {
                        'left': 'left',
                        'right': 'right',
                        'middle': 'middle'
                    }
                    button = button_map.get(content.lower(), 'left')
                    pyautogui.click(button=button)
                    return f"已执行{button}键点击"
            else:
                # 既没有 coordinates 也没有 content，在当前鼠标位置点击
                pyautogui.click()
                return "已在当前位置点击"
        
        elif action_type == "search":
            # 委托给浏览器工具：仅使用 Selenium + 操作后 DOM 获取内容，不使用屏幕分析/坐标逻辑
            if not content:
                return "Error: 'search' action requires 'content' parameter"
            try:
                from .browser_tool import browser_search
                return browser_search(content)
            except ImportError as e:
                return f"Error: 浏览器搜索依赖未就绪: {e}. 请确认 tools.browser_tool 可用"
        
        elif action_type == "write":
            # 在记事本或其他文本编辑器中写入文本 - 使用视觉分析定位编辑区域
            if not content:
                return "Error: 'write' action requires 'content' parameter"
            
            # 【修复】确保窗口激活，失败则直接返回错误
            if target_window:
                if not activate_window(target_window):
                    return f"Error: 无法激活窗口 '{target_window}'，无法执行写入操作"
            
            # 优先使用视觉分析定位编辑区域
            _ensure_smart_automation_loaded()
            if SMART_AUTOMATION_AVAILABLE and find_element_and_type:
                # 尝试查找编辑区域
                edit_description = "编辑区域" if not target_window else f"{target_window}中的编辑区域"
                result = find_element_and_type(
                    element_description=edit_description,
                    text=content,
                    target_window=target_window
                )
                if "已找到" in result or "已在" in result:
                    return result
                # 如果视觉分析失败，回退到传统方式
            
            # 回退：使用传统方式（点击编辑区域中心）
            try:
                screen_width, screen_height = pyautogui.size()
                # 点击编辑区域中心（屏幕中央偏上）
                edit_area_x = screen_width // 2
                edit_area_y = screen_height // 2
                pyautogui.click(edit_area_x, edit_area_y)
                time.sleep(0.3)  # 等待焦点获得
            except:
                pass  # 如果点击失败，继续尝试输入
            
            # 尝试多种方式确保文本能够正确写入
            try:
                # 方式1: 先按Ctrl+A选择所有内容，然后按Delete删除，再写入新内容
                pyautogui.hotkey('ctrl', 'a')
                time.sleep(0.2)
                pyautogui.press('delete')
                time.sleep(0.2)
                pyautogui.write(content, interval=0.05)
                time.sleep(0.1)  # 等待输入完成
                return f"已写入文本: {content[:50]}{'...' if len(content) > 50 else ''}"
            except Exception:
                # 方式2: 直接写入文本（不清空原有内容）
                try:
                    pyautogui.write(content, interval=0.05)
                    time.sleep(0.1)
                    return f"已写入文本: {content[:50]}{'...' if len(content) > 50 else ''}"
                except Exception as e:
                    return f"写入文本失败: {str(e)}"
        
        elif action_type == "draw":
            # 在画图中绘制简单图形
            if not content:
                return "Error: 'draw' action requires 'content' parameter"
            
            content_lower = content.lower()
            
            # 获取屏幕中心作为起始点
            screen_width, screen_height = pyautogui.size()
            center_x, center_y = screen_width // 2, screen_height // 2
            
            # 【修复】辅助函数：确保坐标在屏幕范围内
            def clamp_coord(coord, max_val):
                return max(0, min(coord, max_val - 1))
            
            if "circle" in content_lower or "圆" in content:
                # 画圆
                radius = min(100, min(screen_width, screen_height) // 4)  # 确保半径不超过屏幕的1/4
                import math
                points = []
                for angle in range(0, 360, 10):
                    x = clamp_coord(center_x + int(radius * math.cos(math.radians(angle))), screen_width)
                    y = clamp_coord(center_y + int(radius * math.sin(math.radians(angle))), screen_height)
                    points.append((x, y))
                
                # 按住鼠标拖动画圆
                pyautogui.moveTo(points[0][0], points[0][1])
                pyautogui.mouseDown()
                for x, y in points[1:]:
                    pyautogui.moveTo(x, y, duration=0.01)
                pyautogui.mouseUp()
                return "已绘制圆形"
            
            elif "square" in content_lower or "rect" in content_lower or "方" in content or "矩形" in content:
                # 画矩形
                size = min(150, min(screen_width, screen_height) // 3)  # 确保大小不超过屏幕的1/3
                x1 = clamp_coord(center_x - size//2, screen_width)
                y1 = clamp_coord(center_y - size//2, screen_height)
                x2 = clamp_coord(center_x + size//2, screen_width)
                y2 = clamp_coord(center_y + size//2, screen_height)
                pyautogui.moveTo(x1, y1)
                pyautogui.mouseDown()
                pyautogui.moveTo(x2, y1, duration=0.1)
                pyautogui.moveTo(x2, y2, duration=0.1)
                pyautogui.moveTo(x1, y2, duration=0.1)
                pyautogui.moveTo(x1, y1, duration=0.1)
                pyautogui.mouseUp()
                return "已绘制矩形"
            
            elif "line" in content_lower or "线" in content:
                # 画线
                line_length = min(200, screen_width // 3)  # 确保线长不超过屏幕的1/3
                x1 = clamp_coord(center_x - line_length//2, screen_width)
                y1 = clamp_coord(center_y, screen_height)
                x2 = clamp_coord(center_x + line_length//2, screen_width)
                pyautogui.moveTo(x1, y1)
                pyautogui.mouseDown()
                pyautogui.moveTo(x2, y1, duration=0.2)
                pyautogui.mouseUp()
                return "已绘制直线"
            
            else:
                # 默认：画一个简单的图形
                size = min(100, min(screen_width, screen_height) // 4)
                x1 = clamp_coord(center_x - size//2, screen_width)
                y1 = clamp_coord(center_y - size//2, screen_height)
                x2 = clamp_coord(center_x + size//2, screen_width)
                y2 = clamp_coord(center_y + size//2, screen_height)
                pyautogui.moveTo(x1, y1)
                pyautogui.mouseDown()
                pyautogui.moveTo(x2, y1, duration=0.1)
                pyautogui.moveTo(x2, y2, duration=0.1)
                pyautogui.moveTo(x1, y2, duration=0.1)
                pyautogui.moveTo(x1, y1, duration=0.1)
                pyautogui.mouseUp()
                return f"已绘制图形: {content}"
        
        elif action_type == "wait":
            # 等待指定时间
            if content:
                try:
                    wait_time = float(content)
                    if wait_time < 0:
                        return "Error: Wait time cannot be negative"
                    time.sleep(wait_time)
                    return f"已等待 {wait_time} 秒"
                except (ValueError, TypeError):
                    return f"Error: 'wait' action requires a valid number, received: {content}"
            else:
                # 如果没有提供 content，默认等待1秒
                time.sleep(1.0)
                return "已等待 1 秒"
        
        elif action_type == "move":
            # 移动鼠标
            if content:
                try:
                    x, y = map(int, content.split(','))
                    # 【修复】验证坐标是否在屏幕范围内
                    screen_width, screen_height = pyautogui.size()
                    if x < 0 or x >= screen_width or y < 0 or y >= screen_height:
                        return f"Error: 坐标 ({x}, {y}) 超出屏幕范围 ({screen_width}x{screen_height})"
                    pyautogui.moveTo(x, y, duration=0.5)
                    return f"已移动鼠标到 ({x}, {y})"
                except (ValueError, IndexError):
                    return "Error: 'move' action requires coordinates in format 'x,y'"
            else:
                return "Error: 'move' action requires 'content' parameter with coordinates"
        
        else:
            return f"Error: Unknown action type '{action_type}'. Supported types: type, key, click, search, write, draw, wait, move"
    
    except Exception as e:
        return f"自动化操作失败: {str(e)}"


def automate_sequence(actions):
    """
    执行一系列自动化操作
    
    Args:
        actions (list): 操作列表，每个操作是一个字典，包含 action_type, content 等参数
    
    Returns:
        str: 操作结果描述
    """
    results = []
    for i, action in enumerate(actions):
        action_type = action.get('action_type')
        content = action.get('content')
        target_window = action.get('target_window')
        delay = action.get('delay', 0.5)
        
        result = automate_action(action_type, content, target_window, delay)
        results.append(f"步骤 {i+1}: {result}")
        
        # 操作之间的额外延迟
        if i < len(actions) - 1:
            time.sleep(delay)
    
    return "\n".join(results)


if __name__ == "__main__":
    # 测试
    print(automate_action("type", "Hello World"))
    time.sleep(1)
    print(automate_action("key", "enter"))
