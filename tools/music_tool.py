"""
网易云音乐操作工具
支持打开应用、搜索音乐、播放音乐、播放歌单中的歌曲等操作
使用 uiautomation 进行自动化操作
"""
import time
import os
import sys
import re
import functools

# 添加项目根目录到 sys.path，以便导入 config
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

try:
    import pyautogui
    PY_AUTOGUI_AVAILABLE = True
except ImportError:
    PY_AUTOGUI_AVAILABLE = False

try:
    import uiautomation as uia
    UI_AUTOMATION_AVAILABLE = True
except ImportError:
    UI_AUTOMATION_AVAILABLE = False
    
# 尝试导入 comtypes 以便手动管理 COM 初始化（作为备份）
try:
    import comtypes
except ImportError:
    comtypes = None


# 导入大模型API配置
try:
    import requests
    from config import (
        DEEPSEEK_API_URL,
        DEEPSEEK_THINKING_API_KEY,
        DEEPSEEK_MODEL,
        DEEPSEEK_API_TIMEOUT,
        require_env,
    )
    LLM_API_AVAILABLE = True
except ImportError as e:
    LLM_API_AVAILABLE = False
    print(f"警告: 无法导入大模型API配置，将使用备用方法 - {e}")


MAIN_WINDOW_NAME = 'OrpheusBrowserHost'
MAIN_WINDOW = None
ANCHOR_NAME = '听歌识曲'
ANCHOR = None
ALL_LIKES = None  # 歌单列表容器（通过"我喜欢的音乐"组的父组件的父组件定位）
PLAYLIST_ANCHOR = 0  # 当前定位到的歌单索引锚点
PLAY_LIST = []  # 全局歌单列表缓存
INTERVAL = 0.25
MAX_SEARCH_DEPTH = 50


def require_uia(func):
    """
    装饰器：确保 COM 在线程中初始化，并处理 uiautomation 的上下文。
    这对于在 worker 线程中运行 uiautomation 是必须的。
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not UI_AUTOMATION_AVAILABLE:
            return "失败: 未安装 uiautomation 库"
            
        # 1. 优先使用 uiautomation 提供的线程初始化器
        if hasattr(uia, "UIAutomationInitializerInThread"):
            # UIAutomationInitializerInThread 会自动调用 CoInitialize 和 CoUninitialize
            try:
                with uia.UIAutomationInitializerInThread(debug=False):
                    return func(*args, **kwargs)
            except Exception as e:
                return f"执行出错(UIA Thread): {str(e)}"
        
        # 2. 回退方案：手动初始化 COM
        need_uninit = False
        if comtypes:
            try:
                comtypes.CoInitialize()
                need_uninit = True
            except Exception:
                # 已经初始化过，或者不支持
                pass
        
        try:
            return func(*args, **kwargs)
        except Exception as e:
            return f"执行出错(Manual COM): {str(e)}"
        finally:
            if need_uninit and comtypes:
                try:
                    comtypes.CoUninitialize()
                except:
                    pass
    return wrapper


def _find_control_depth(root_control, target_control, max_depth=50):
    """
    查找目标控件在根控件树中的深度
    
    Args:
        root_control: 根控件
        target_control: 目标控件
        max_depth: 最大搜索深度
    
    Returns:
        int: 深度（从0开始），如果找不到返回-1
    """
    try:
        # 首先尝试使用 Handle 属性比较（如果存在）
        if hasattr(root_control, 'Handle') and hasattr(target_control, 'Handle'):
            try:
                if root_control.Handle == target_control.Handle:
                    return 0
            except:
                pass
        
        # 如果 Handle 比较失败或不存在，尝试使用 Name 和 ControlTypeName 比较
        if (hasattr(root_control, 'Name') and hasattr(target_control, 'Name') and
            hasattr(root_control, 'ControlTypeName') and hasattr(target_control, 'ControlTypeName')):
            try:
                if (root_control.Name == target_control.Name and
                    root_control.ControlTypeName == target_control.ControlTypeName):
                    return 0
            except:
                pass
    except:
        pass
    
    def find_depth_recursive(ctrl, target, current_depth, max_d):
        if current_depth > max_d:
            return -1
        try:
            children = ctrl.GetChildren()
            for child in children:
                # 尝试使用 Handle 比较
                try:
                    if hasattr(child, 'Handle') and hasattr(target, 'Handle'):
                        if child.Handle == target.Handle:
                            return current_depth + 1
                    else:
                        # 如果没有 Handle，使用 Name 和 ControlTypeName 比较
                        if (hasattr(child, 'Name') and hasattr(target, 'Name') and
                            hasattr(child, 'ControlTypeName') and hasattr(target, 'ControlTypeName')):
                            if (child.Name == target.Name and 
                                child.ControlTypeName == target.ControlTypeName):
                                return current_depth + 1
                except:
                    pass
                result = find_depth_recursive(child, target, current_depth + 1, max_d)
                if result != -1:
                    return result
        except:
            pass
        return -1
    
    try:
        return find_depth_recursive(root_control, target_control, 0, max_depth)
    except:
        return -1


def _ensure_netease_window_active():
    """
    确保网易云音乐主窗口处于活动状态
    如果窗口不存在，则打开网易云音乐应用
    如果窗口最小化，则恢复窗口
    
    注意：该函数通常被外部已经 decorated 的函数调用，所以这里直接获取即可。
    为了保险，我们还是重新获取一下控件对象，因为 COM 指针可能跨线程无效。
    """
    global MAIN_WINDOW, ANCHOR
    
    MAIN_WINDOW = uia.WindowControl(ClassName='OrpheusBrowserHost', searchDepth=1)
    if MAIN_WINDOW.Exists():
        try:
            # 尝试恢复窗口（如果最小化，这会恢复它；如果已经正常，不会有副作用）
            # uiautomation 使用 win32gui 的 ShowWindow，SW_RESTORE = 9
            try:
                try:
                    import win32gui
                    import win32con
                    hwnd = None
                    if hasattr(MAIN_WINDOW, 'Handle'):
                        hwnd = MAIN_WINDOW.Handle
                    if hwnd:
                        # 检查窗口是否最小化
                        placement = win32gui.GetWindowPlacement(hwnd)
                        if placement[1] == win32con.SW_SHOWMINIMIZED:
                            # 窗口最小化，恢复它
                            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                            # 等待窗口恢复：检测窗口是否可见
                            if not MAIN_WINDOW.Exists(3, 0.1):
                                # 如果窗口不存在，等待一下再检测
                                for _ in range(30):  # 最多等待3秒
                                    if MAIN_WINDOW.Exists(0.1, 0.1):
                                        break
                except ImportError:
                    # 如果没有 win32gui，尝试使用 uiautomation 的方法
                    try:
                        # uiautomation 的 ShowWindow 方法
                        MAIN_WINDOW.ShowWindow(9)  # SW_RESTORE = 9
                        # 等待窗口恢复
                        if not MAIN_WINDOW.Exists(3, 0.1):
                            for _ in range(30):
                                if MAIN_WINDOW.Exists(0.1, 0.1):
                                    break
                    except:
                        pass
                except Exception:
                    # 如果检查失败，至少尝试恢复窗口
                    try:
                        MAIN_WINDOW.ShowWindow(9)
                        if not MAIN_WINDOW.Exists(3, 0.1):
                            for _ in range(30):
                                if MAIN_WINDOW.Exists(0.1, 0.1):
                                    break
                    except:
                        pass
            except Exception:
                # 如果所有恢复方法都失败，继续尝试激活窗口
                pass
            
            # 激活窗口
            MAIN_WINDOW.SetActive()
            # 等待窗口激活：检测ANCHOR是否可见
            # 重新查找ANCHOR，因为窗口可能刚激活
            ANCHOR = None
            for _ in range(30):  # 最多等待3秒
                try:
                    ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH)
                    if ANCHOR.Exists(0.1, 0.1):
                        break
                except:
                    pass
                time.sleep(0.1)
            
            # 如果还是找不到，尝试更大的搜索深度
            if not ANCHOR or not ANCHOR.Exists(0.1, 0.1):
                try:
                    ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH*2)
                except:
                    pass
        except Exception as e:
            # 如果恢复失败，至少尝试激活
            try:
                MAIN_WINDOW.SetActive()
                # 重新查找ANCHOR
                ANCHOR = None
                for _ in range(30):
                    try:
                        ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH)
                        if ANCHOR.Exists(0.1, 0.1):
                            break
                    except:
                        pass
                    time.sleep(0.1)
                
                # 如果还是找不到，尝试更大的搜索深度
                if not ANCHOR or not ANCHOR.Exists(0.1, 0.1):
                    try:
                        ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH*2)
                    except:
                        pass
            except:
                print(f"警告: 无法激活窗口: {e}")
    else:
        # 打开应用操作不依赖 UIA，可以直接调用，但在 wrapper 内调用更安全
        _open_netease_music_exe()
        MAIN_WINDOW = uia.WindowControl(ClassName='OrpheusBrowserHost', searchDepth=1)  # 等待应用启动后再次尝试获取窗口
        if MAIN_WINDOW.Exists(10, 0.5):  # 增加等待时间
            try:
                MAIN_WINDOW.SetActive()
                # 等待ANCHOR出现
                ANCHOR = None
                for _ in range(50):  # 最多等待5秒（应用刚启动需要更长时间）
                    try:
                        ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH)
                        if ANCHOR.Exists(0.1, 0.1):
                            break
                    except:
                        pass
                    time.sleep(0.1)
                
                # 如果还是找不到，尝试更大的搜索深度
                if not ANCHOR or not ANCHOR.Exists(0.1, 0.1):
                    try:
                        ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH*2)
                    except:
                        pass
            except:
                pass
    
    # 最后尝试一次查找ANCHOR（如果之前都没找到）
    if not ANCHOR or not ANCHOR.Exists(0.1, 0.1):
        try:
            ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH*2)
        except:
            # 如果还是找不到，打印警告但不抛出异常
            print("警告: 找不到听歌识曲控件，某些功能可能无法正常工作")


def _open_netease_music_exe():
    """内部函数：仅执行启动 EXE，不涉及 UIA 操作"""
    import subprocess
    exe_path = r"D:\CloudMusic\cloudmusic.exe"
    
    # 尝试查找常见安装路径
    possible_paths = [
        r"D:\CloudMusic\cloudmusic.exe",
        r"C:\Program Files (x86)\Netease\CloudMusic\cloudmusic.exe",
        r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe",
        r"E:\CloudMusic\cloudmusic.exe"
    ]
    
    target_path = None
    if os.path.exists(exe_path):
        target_path = exe_path
    else:
        for path in possible_paths:
            if os.path.exists(path):
                target_path = path
                break
                
    if not target_path:
        raise FileNotFoundError(f"找不到网易云音乐可执行文件，请检查安装路径。尝试路径: {exe_path}")
    
    # 使用 --force-renderer-accessibility 参数启动应用以确保正确定位 ui 元素
    subprocess.Popen([target_path, "--force-renderer-accessibility"], shell=False)


@require_uia
def open_netease_music():
    """
    打开网易云音乐应用 (Public Entry)
    
    Returns:
        str: 操作结果描述
    """
    try:
        # 检查是否已经运行
        temp_window = uia.WindowControl(ClassName='OrpheusBrowserHost', searchDepth=1)
        if temp_window.Exists(1, 0):
             return "成功: 网易云音乐应用已在运行"
             
        _open_netease_music_exe()
        # 等待应用启动：检测主窗口是否出现
        for _ in range(60):  # 最多等待6秒
            # 在循环中每次重新创建对象，因为状态可能变化
            MAIN_WINDOW = uia.WindowControl(ClassName='OrpheusBrowserHost', searchDepth=1)
            if MAIN_WINDOW.Exists(0.1, 0.1):
                break
            time.sleep(0.1)
        return f"成功: 已启动网易云音乐应用"
    except Exception as e:
        return f"启动网易云音乐应用时出错: {str(e)}"
    
def _click_left_edge_and_home():
    """
    点击窗口左侧边界线中心点旁的位置（窗口内与边界线相邻的安全区域），然后发送HOME键
    
    用于正确识别锚点，在获取歌单名称和切换歌单时使用
    """
    global MAIN_WINDOW
    
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        print("警告: 主窗口不存在，跳过点击左侧边界和HOME键操作")
        return
    
    try:
        # 获取窗口的位置和大小
        rect = MAIN_WINDOW.BoundingRectangle
        if not rect:
            print("警告: 无法获取窗口位置信息，跳过点击左侧边界和HOME键操作")
            return
        
        # 获取窗口边界（uiautomation 的 BoundingRectangle 是一个 Rect 对象）
        try:
            left = rect.left
            top = rect.top
            width = rect.width()
            height = rect.height()
        except AttributeError:
            # 如果 BoundingRectangle 是元组或其他格式，尝试其他方式
            try:
                left = rect[0]
                top = rect[1]
                width = rect[2] - rect[0]
                height = rect[3] - rect[1]
            except (TypeError, IndexError):
                print("警告: 无法解析窗口位置信息，跳过点击左侧边界和HOME键操作")
                return
        
        if width == 0 or height == 0:
            print("警告: 窗口大小为0，跳过点击左侧边界和HOME键操作")
            return
        
        # 计算左侧边界线中心点
        left_edge_x = left
        center_y = top + height // 2
        
        # 点击窗口内与边界线相邻的安全区域（边界线右侧8像素的位置）
        safe_x = left_edge_x + 8  # 边界线右侧8像素，确保在窗口内
        
        # 使用 pyautogui 点击（如果可用）
        if PY_AUTOGUI_AVAILABLE:
            try:
                pyautogui.click(safe_x, center_y)
                time.sleep(0.1)  # 短暂等待点击完成
                # 发送HOME键
                pyautogui.press('home')
                time.sleep(0.1)  # 短暂等待HOME键完成
                print(f"已点击窗口左侧边界旁位置 ({safe_x}, {center_y}) 并发送HOME键")
            except Exception as e:
                print(f"使用 pyautogui 点击左侧边界时出错: {e}")
                # 如果 pyautogui 失败，尝试使用 uiautomation
                try:
                    # 使用 uiautomation 的 Click 方法（使用相对坐标）
                    rel_x = 8  # 距离左边界8像素
                    rel_y = height // 2  # 窗口中心高度
                    MAIN_WINDOW.Click(rel_x, rel_y)
                    time.sleep(0.1)
                    MAIN_WINDOW.SendKeys('{HOME}')
                    time.sleep(0.1)
                    print(f"已使用 uiautomation 点击窗口左侧边界旁位置并发送HOME键")
                except Exception as e2:
                    print(f"使用 uiautomation 点击左侧边界时出错: {e2}")
        else:
            # 如果没有 pyautogui，使用 uiautomation
            try:
                # 计算相对坐标
                rel_x = 8  # 距离左边界8像素
                rel_y = height // 2  # 窗口中心高度
                MAIN_WINDOW.Click(rel_x, rel_y)
                time.sleep(0.1)
                MAIN_WINDOW.SendKeys('{HOME}')
                time.sleep(0.1)
                print(f"已使用 uiautomation 点击窗口左侧边界旁位置并发送HOME键")
            except Exception as e:
                print(f"使用 uiautomation 点击左侧边界时出错: {e}")
    except Exception as e:
        print(f"点击左侧边界和发送HOME键时出错: {e}")


def dropdown():
    """
    点击dropdown
    """
    # 1. 检查主窗口
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(1, 0):
        print("错误: 找不到网易云音乐主窗口")
        return None
    
    print("正在查找 dropdown 按钮 (深度: 50)...")
    
    try:
        # 【关键修改】直接使用 ButtonControl 查找，并强制设置 searchDepth=50
        # 这比 find_all + lambda 更快，且不容易漏掉
        dropdown_btn = MAIN_WINDOW.ButtonControl(Name='dropdown', searchDepth=MAX_SEARCH_DEPTH)
        
        # 快速检查是否存在
        if dropdown_btn.Exists(1, 0): # 等待最多1秒
            # 查找深度信息
            depth = _find_control_depth(MAIN_WINDOW, dropdown_btn)
            print(f"找到控件: {dropdown_btn.Name}, 类型: {dropdown_btn.ControlTypeName}, 深度: {depth}")
            
            # 尝试点击
            try:
                # 优先尝试 Invoke 模式（更底层，不受鼠标遮挡影响）
                if dropdown_btn.GetInvokePattern():
                    dropdown_btn.GetInvokePattern().Invoke()
                else:
                    dropdown_btn.Click()
            except:
                # 如果出错则回退到普通点击
                dropdown_btn.Click()
                
            return dropdown_btn.Name
        else:
            # 如果 ButtonControl 找不到，尝试找 ImageControl 或者泛型 Control
            print("未找到 ButtonControl，尝试查找通用 Control...")
            any_dropdown = MAIN_WINDOW.Control(Name='dropdown', searchDepth=MAX_SEARCH_DEPTH)
            if any_dropdown.Exists(1, 0):
                 depth = _find_control_depth(MAIN_WINDOW, any_dropdown)
                 print(f"找到了名为 dropdown 的控件，但类型是: {any_dropdown.ControlTypeName}, 深度: {depth}")
                 any_dropdown.Click()
                 return any_dropdown.Name
            else:
                 print("彻底未找到名为 'dropdown' 的控件")
            
    except Exception as e:
        print(f"点击dropdown时出错: {e}")

    return None

@require_uia
def search_music_in_netease(query):
    """
    在网易云音乐主界面中搜索音乐
    
    Args:
        query (str): 搜索关键词（歌曲名、歌手名等）
    
    Returns:
        str: 操作结果描述
    """
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        _ensure_netease_window_active()
    
    dropdown()
    
    # 确保 ANCHOR 存在
    global ANCHOR
    if not ANCHOR or not ANCHOR.Exists(3, 1):
        # 重新查找 ANCHOR
        try:
            ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH)
            if not ANCHOR.Exists(3, 1):
                return "失败: 找不到听歌识曲控件（搜索栏）"
        except Exception as e:
            return f"失败: 无法定位听歌识曲控件 - {str(e)}"
    
    try:
        top_container = ANCHOR.GetParentControl()
        if not top_container:
            return "失败: 无法获取搜索栏的父容器"
        
        search_box = top_container.EditControl()
        if not search_box.Exists(3, INTERVAL):
            return "失败: 找不到搜索框"
        
        search_box.Click()
        # 使用 ValuePattern 设置文本，如果没有则使用 SendKeys
        try:
            # 尝试使用 ValuePattern
            value_pattern = search_box.GetValuePattern()
            if value_pattern:
                value_pattern.SetValue(query)
                print(f"使用 ValuePattern 设置文本: {query}")
            else:
                raise AttributeError("ValuePattern not available")
        except (AttributeError, Exception) as e:
            # 如果 ValuePattern 失败，使用 SendKeys（先清空再输入）
            print(f"ValuePattern 不可用，使用 SendKeys: {e}")
            search_box.SendKeys('^a')  # Ctrl+A 全选
            time.sleep(0.1)  # 短暂等待
            search_box.SendKeys(query)
        search_box.SendKeys('{ENTER}')
        return f"成功: 已搜索音乐 '{query}'"
    except Exception as e:
        return f"失败: 搜索音乐时出错 - {str(e)}"
    else:
        return f"失败: 找不到听歌识曲控件"


@require_uia
def play_music_in_netease(query=None, play_first_result=True, result_index=0):
    """
    在网易云音乐主页面中搜索并播放音乐
    
    Args:
        query (str, optional): 搜索关键词。如果提供，会先检查屏幕上是否存在，不存在则搜索
        play_first_result (bool): 是否播放搜索结果，默认True
        result_index (int): 播放第几个搜索结果（0表示第一首），默认0
    
    Returns:
        str: 操作结果描述
    """
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        _ensure_netease_window_active()
    
    dropdown()
    search_music_in_netease(query)
    
    # 新的逻辑：先点击"单曲"选项卡，然后查找"play 播放全部"按钮
    try:
        # 1. 先点击"单曲"选项卡项目
        single_songs_tab = MAIN_WINDOW.TabItemControl(Name='单曲', searchDepth=MAX_SEARCH_DEPTH)
        if not single_songs_tab.Exists(3, INTERVAL):
            return "失败: 找不到'单曲'选项卡项目"
        
        depth = _find_control_depth(MAIN_WINDOW, single_songs_tab, MAX_SEARCH_DEPTH)
        print(f"找到'单曲'选项卡，深度: {depth}")
        
        try:
            single_songs_tab.Click()
            print("已点击'单曲'选项卡")
            # 等待选项卡切换完成
            time.sleep(0.5)
        except Exception as e:
            return f"失败: 点击'单曲'选项卡时出错 - {str(e)}"
        
        # 2. 查找"play 播放全部"按钮
        play_all_buttons = []
        found_depths = []
        def find_play_all(ctrl, depth=0, max_d=20):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ButtonControl":
                        name = (child.Name or "").strip()
                        if name == "play 播放全部" or name.lower() == "play 播放全部" or "播放全部" in name:
                            play_all_buttons.append(child)
                            found_depths.append(depth + 1)
                            print(f"找到播放全部按钮 (深度: {depth + 1}, 名称: {name})")
                    find_play_all(child, depth + 1, max_d)
            except:
                pass
        find_play_all(MAIN_WINDOW, 0, 20)
        
        if not play_all_buttons:
            return "失败: 找不到'play 播放全部'按钮"
        
        play_all_button = play_all_buttons[0]
        if not play_all_button.Exists(3, INTERVAL):
            return "失败: 'play 播放全部'按钮不可访问"
        
        # 2. 在该按钮的同级定位最后一个子组件
        play_all_parent = play_all_button.GetParentControl()
        if not play_all_parent:
            return "失败: 无法获取'play 播放全部'按钮的父控件"
        
        siblings = play_all_parent.GetChildren()
        if not siblings:
            return "失败: 父控件没有子组件"
        
        # 找到最后一个子组件
        last_sibling = siblings[-1]
        print(f"找到最后一个同级子组件，类型: {last_sibling.ControlTypeName}, 名称: {last_sibling.Name or 'N/A'}")
        
        # 3. 在该子组件中搜索 Name="grid" 的 TableControl
        grid_table = None
        found_depth_grid = -1
        def find_grid_table(ctrl, depth=0, max_d=10):
            nonlocal grid_table, found_depth_grid
            if depth > max_d or grid_table:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    # 精确匹配：Name="grid" 且 ControlTypeName="TableControl"
                    if (child.ControlTypeName == "TableControl" and 
                        (child.Name or "").strip() == "grid"):
                        grid_table = child
                        found_depth_grid = depth + 1
                        print(f"找到 grid 表格 (深度: {depth + 1}, 类型: {child.ControlTypeName}, 名称: {child.Name})")
                        return
                    find_grid_table(child, depth + 1, max_d)
            except:
                pass
        find_grid_table(last_sibling, 0, 10)
        
        if not grid_table:
            return "失败: 在最后一个子组件中找不到 Name='grid' 的 TableControl"
        
        if not grid_table.Exists(3, INTERVAL):
            return "失败: grid 表格不可访问"
        
        # 4. "grid"表格的第一个子节点为"行组"（Name="" 且 ControlTypeName="GroupControl"）
        grid_children = grid_table.GetChildren()
        if not grid_children:
            return "失败: grid 表格没有子节点"
        
        first_row_group = None
        for child in grid_children:
            # 精确匹配：Name="" 且 ControlTypeName="GroupControl"
            if (child.ControlTypeName == "GroupControl" and 
                (child.Name or "").strip() == ""):
                first_row_group = child
                break
        
        if not first_row_group:
            return "失败: grid 表格的第一个子节点不是空名称的 GroupControl（行组）"
        
        print(f"找到第一个行组，类型: {first_row_group.ControlTypeName}, 名称: '{first_row_group.Name or ''}'")
        
        if not first_row_group.Exists(3, INTERVAL):
            return "失败: 第一个行组不可访问"
        
        # 5. 双击该"行组"中的第一个子组
        row_group_children = first_row_group.GetChildren()
        if not row_group_children:
            return "失败: 行组中没有子组"
        
        first_sub_group = row_group_children[0]
        print(f"找到第一个子组，类型: {first_sub_group.ControlTypeName}, 名称: {first_sub_group.Name or 'N/A'}")
        
        if not first_sub_group.Exists(3, INTERVAL):
            return "失败: 第一个子组不可访问"
        
        first_sub_group.DoubleClick()
        return f"成功: 已播放音乐 '{query}'"
        
    except Exception as e:
        return f"失败: 播放音乐时出错 - {str(e)}"

@require_uia
def get_playlist_name_in_netease(force_refresh=False):
    """
    获取网易云音乐歌单名称
    
    逻辑：
    1. 定位容器（通过"我喜欢的音乐"组找到其父组件的父组件）
    2. 在容器中搜索三个区域：
       - "我喜欢的音乐"组
       - "收藏的歌单"区域（包含收藏的歌单）
       - "创建的歌单"区域（包含sidebar_add组）
    3. 对于收藏和创建的歌单，通过点击最后一个名称来触发刷新，直到出现重复项
    
    Args:
        force_refresh (bool): 是否强制刷新缓存，默认False。如果为True，即使有缓存也会重新获取
    
    Returns:
        list: 所有歌单名称列表
    """
    global PLAY_LIST, ALL_LIKES
    
    # 如果已经缓存过且不需要强制刷新，直接返回缓存结果
    if PLAY_LIST and not force_refresh:
        return PLAY_LIST
    
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        _ensure_netease_window_active()
    
    dropdown()
    
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        return []
    
    # 在进行原有逻辑之前，点击窗口左侧边界线中心点旁的位置，然后发送HOME键以正确识别锚点
    _click_left_edge_and_home()
    
    # 1. 定位容器
    container = None
    try:
        # 通过"我喜欢的音乐"组找到容器
        # 直接使用 Control 查找 AutomationId 为 "left_nav_myFavoriteMusic" 的控件
        control = MAIN_WINDOW.Control(AutomationId="left_nav_myFavoriteMusic", searchDepth=MAX_SEARCH_DEPTH)
        if control.Exists(1, 0):
            depth = _find_control_depth(MAIN_WINDOW, control)
            print(f"找到 'left_nav_myFavoriteMusic' 控件，深度: {depth}")
            controls = [control]
        else:
            controls = []
        if controls and len(controls) > 0:
            control = controls[0]
            # 获取父组件的父组件（容器）
            parent = control.GetParentControl()  # 父组件
            if parent:
                container = parent.GetParentControl()  # 父组件的父组件（容器）
        
        if not container:
            print("错误: 找不到容器")
            return []
        
        # 保存容器到全局变量
        ALL_LIKES = container
    except Exception as e:
        print(f"定位容器时出错: {e}")
        return []
    
    # 2. 在容器中搜索歌单名称
    playlist_names = []
    playlist_names_set = set()
    
    # 2.1 获取"我喜欢的音乐"
    try:
        # 使用 FindAll 查找 AutomationId 为 "left_nav_myFavoriteMusic" 的控件
        all_likes_control_obj = container.Control(AutomationId="left_nav_myFavoriteMusic", searchDepth=MAX_SEARCH_DEPTH)
        if all_likes_control_obj.Exists(1, 0):
            depth = _find_control_depth(container, all_likes_control_obj, MAX_SEARCH_DEPTH)
            print(f"找到 'left_nav_myFavoriteMusic' 控件，深度: {depth}")
            all_likes_control = [all_likes_control_obj]
        else:
            all_likes_control = []
        if all_likes_control and len(all_likes_control) > 0:
            all_likes_name = "我喜欢的音乐"
            if all_likes_name not in playlist_names_set:
                playlist_names.append(all_likes_name)
                playlist_names_set.add(all_likes_name)
    except Exception as e:
        print(f"获取'我喜欢的音乐'时出错: {e}")
    
    # 2.2 获取创建的歌单
    try:
        # 计算创建的歌单在整个列表中的起始索引（"我喜欢的音乐"之后）
        created_start_index = len(playlist_names)
        created_names = _get_created_playlists(container, playlist_names_set, playlist_names, created_start_index)
        for name in created_names:
            if name not in playlist_names_set:
                playlist_names.append(name)
                playlist_names_set.add(name)
    except Exception as e:
        print(f"获取创建的歌单时出错: {e}")

    # 2.3 获取收藏的歌单
    try:
        # 计算收藏的歌单在整个列表中的起始索引（"我喜欢的音乐" + 创建的歌单之后）
        collected_start_index = len(playlist_names)
        collected_names = _get_collected_playlists(container, playlist_names_set, playlist_names, collected_start_index)
        for name in collected_names:
            if name not in playlist_names_set:
                playlist_names.append(name)
                playlist_names_set.add(name)
    except Exception as e:
        print(f"获取收藏的歌单时出错: {e}")
    
    # 3. 保存结果
    PLAY_LIST = playlist_names.copy()
    return playlist_names


def _get_collected_playlists(container, existing_names_set, all_playlist_names, start_index):
    """
    获取收藏的歌单名称
    
    逻辑：
    1. 在容器中查找包含"收藏的歌单"文本的组
    2. 在该组的第一级子组件中，找到最后一个""组
    3. 在该""组中提取歌单名称
    4. 点击最后一个名称，等待刷新，继续提取，直到出现重复项
    
    Args:
        container: 容器控件
        existing_names_set: 已存在的名称集合（用于去重）
        all_playlist_names: 整个包含三个类别的歌单名称列表（用于计算绝对索引）
        start_index: 当前类别在整个列表中的起始索引
    
    Returns:
        list: 收藏的歌单名称列表
    """
    collected_names = []
    collected_names_set = set()
    
    try:
        # 1. 查找包含"收藏的歌单"文本的组
        # 使用 GetChildren 递归查找 GroupControl
        all_groups = []
        def collect_groups(ctrl, depth=0, max_d=10):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "GroupControl":
                        all_groups.append(child)
                    collect_groups(child, depth + 1, max_d)
            except:
                pass
        collect_groups(container, 0, 10)
        collected_section_group = None
        
        for group in all_groups:
            # 检查该组或其子控件是否包含"收藏的歌单"文本
            if _contains_text(group, "收藏的歌单"):
                collected_section_group = group
                break
        
        if not collected_section_group:
            return collected_names
        
        # 2. 在该组的第一级子组件中，找到最后一个""组
        children = collected_section_group.GetChildren()
        if not children:
            return collected_names
        
        # 找到最后一个GroupControl子组件
        last_group = None
        for child in reversed(children):
            if child.ControlTypeName == "GroupControl":
                last_group = child
                break
        
        if not last_group:
            return collected_names
        
        # 3. 在该""组中提取歌单名称，使用键盘滚动 + 点击刷新
        # 先点击"我喜欢的音乐"或任意一个歌单来激活列表
        try:
            # 尝试点击"我喜欢的音乐"来激活列表
            all_likes_control_obj = container.Control(AutomationId="left_nav_myFavoriteMusic", searchDepth=MAX_SEARCH_DEPTH)
            if all_likes_control_obj.Exists(1, 0):
                depth = _find_control_depth(container, all_likes_control_obj, MAX_SEARCH_DEPTH)
                print(f"找到 'left_nav_myFavoriteMusic' 控件，深度: {depth}")
                all_likes_controls = [all_likes_control_obj]
            else:
                all_likes_controls = []
            if all_likes_controls:
                all_likes_controls[0].Click()
                # 等待激活：检测列表是否可滚动
                if not last_group.Exists(1, 0.1):
                    for _ in range(10):
                        if last_group.Exists(0.1, 0.1):
                            break
        except:
            # 如果点击"我喜欢的音乐"失败，尝试点击last_group中的第一个歌单
            try:
                # 使用 GetChildren 递归查找 GroupControl
                playlist_groups = []
                def collect_playlist_groups(ctrl, depth=0, max_d=5):
                    if depth > max_d:
                        return
                    try:
                        children = ctrl.GetChildren()
                        for child in children:
                            if child.ControlTypeName == "GroupControl":
                                playlist_groups.append(child)
                            collect_playlist_groups(child, depth + 1, max_d)
                    except:
                        pass
                collect_playlist_groups(last_group, 0, 5)
                if playlist_groups:
                    playlist_groups[0].Click()
                    # 等待激活：检测列表是否可访问
                    if not last_group.Exists(1, 0.1):
                        for _ in range(10):
                            if last_group.Exists(0.1, 0.1):
                                break
            except:
                pass
        
        max_iterations = 50  # 增加迭代次数，因为需要滚动
        iteration = 0
        last_clicked_index = -1  # 记录上一次点击的歌单在已获取列表中的绝对位置索引
        
        while iteration < max_iterations:
            # 使用键盘向下键滚动（每次按5次）
            try:
                # for _ in range(5):
                last_group.SendKeys('{DOWN 5}')
                # 等待滚动完成：检测列表内容是否变化
                before_visible = _extract_playlist_names_from_group(last_group)
                for _ in range(10):  # 最多等待1秒
                    after_visible = _extract_playlist_names_from_group(last_group)
                    if after_visible != before_visible:
                        break
            except Exception as e:
                print(f"键盘滚动收藏歌单时出错: {e}")
                break
            
            # 每次滚动后都进行点击（点击上一次点击的后面第 8 个，基于已获取列表的绝对位置）
            print("滚动后点击上一次点击的歌单后面的第五个刷新...")
            try:
                # 收集所有可见的歌单控件
                playlist_groups = []
                def collect_playlist_groups(ctrl, depth=0, max_d=5):
                    if depth > max_d:
                        return
                    try:
                        children = ctrl.GetChildren()
                        for child in children:
                            if child.ControlTypeName == "GroupControl":
                                child_name = (child.Name or "").strip()
                                if child_name:  # 只收集有名称的 GroupControl
                                    playlist_groups.append(child)
                            collect_playlist_groups(child, depth + 1, max_d)
                    except:
                        pass
                collect_playlist_groups(last_group, 0, 5)
                
                if not playlist_groups:
                    print("未找到任何歌单控件，退出")
                    break
                
                target_playlist = None
                if last_clicked_index >= 0:
                    # 基于整个列表的绝对位置，计算目标位置
                    target_index = last_clicked_index + 8
                    if target_index < len(all_playlist_names):
                        target_name = all_playlist_names[target_index]
                        # 在当前可见的控件中查找对应名称的控件
                        for group in playlist_groups:
                            group_name = (group.Name or "").strip()
                            if group_name:
                                clean_group_name = _remove_special_chars(group_name)
                                if clean_group_name == target_name:
                                    target_playlist = group
                                    break
                else:
                    # 第一次点击，点击第一个歌单
                    if playlist_groups:
                        target_playlist = playlist_groups[0]
                
                if target_playlist:
                    # 点击目标歌单
                    target_playlist.Click()
                    target_name = (target_playlist.Name or "").strip()
                    clean_target_name = _remove_special_chars(target_name)
                    # 更新记录：找到点击的歌单在整个列表中的绝对位置
                    if clean_target_name in all_playlist_names:
                        last_clicked_index = all_playlist_names.index(clean_target_name)
                    else:
                        # 如果还没在列表中，使用当前整个列表长度作为位置（会在后续添加到列表）
                        last_clicked_index = len(all_playlist_names)
                    print(f"已点击歌单 (整个列表绝对位置: {last_clicked_index}): {target_name}")
                    
                    # 再滚动一次（5次down）
                    try:
                        # for _ in range(5):
                        last_group.SendKeys('{DOWN 5}')
                        # 等待滚动完成
                        before_visible = _extract_playlist_names_from_group(last_group)
                        for _ in range(10):
                            after_visible = _extract_playlist_names_from_group(last_group)
                            if after_visible != before_visible:
                                break
                    except Exception as e:
                        print(f"点击后滚动时出错: {e}")
                else:
                    print(f"无法找到目标歌单 (目标绝对位置: {last_clicked_index + 5 if last_clicked_index >= 0 else 0})，退出")
                    break
            except Exception as e:
                print(f"点击刷新收藏歌单时出错: {e}")
                break
            
            # 提取当前可见的歌单名称并检查是否有新名称
            current_names = _extract_playlist_names_from_group(last_group)
            before_count = len(collected_names)
            
            for name in current_names:
                clean_name = _remove_special_chars(name).strip()
                # 检查是否在类别内已存在，以及是否在整个列表和已存在集合中已存在
                if clean_name and clean_name not in collected_names_set and clean_name not in existing_names_set:
                    collected_names.append(clean_name)
                    collected_names_set.add(clean_name)
                    existing_names_set.add(clean_name)  # 同步更新已存在集合
                    # 同步更新整个列表，确保索引计算准确
                    if clean_name not in all_playlist_names:
                        all_playlist_names.append(clean_name)
            
            after_count = len(collected_names)
            
            # 检查是否有新名称，如果没有新名称，判定为彻底无刷新，退出
            if after_count <= before_count:
                print("点击后仍没有新名称，判定为歌单名称全部获取完毕")
                break
            else:
                print(f"点击后获取到 {after_count - before_count} 个新名称，继续...")
            
            iteration += 1
    
    except Exception as e:
        print(f"获取收藏的歌单时出错: {e}")
    
    return collected_names


def _get_created_playlists(container, existing_names_set, all_playlist_names, start_index):
    """
    获取创建的歌单名称
    
    新逻辑：
    1. 直接查找 Name="sidebar_add" 的 GroupControl
    2. 在其下一个兄弟节点中的组的 Name 即为创建的歌单名称
    
    Args:
        container: 容器控件
        existing_names_set: 已存在的名称集合（用于去重）
        all_playlist_names: 整个包含三个类别的歌单名称列表（用于计算绝对索引）
        start_index: 当前类别在整个列表中的起始索引
    
    Returns:
        list: 创建的歌单名称列表
    """
    created_names = []
    created_names_set = set()
    
    try:
        # 1. 直接查找 Name="sidebar_add" 的 GroupControl
        sidebar_add_group = None
        found_depth = -1
        def find_sidebar_add(ctrl, depth=0, max_d=MAX_SEARCH_DEPTH):
            nonlocal sidebar_add_group, found_depth
            if depth > max_d or sidebar_add_group:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    # 精确匹配：Name="sidebar_add" 且 ControlTypeName="GroupControl"
                    if (child.ControlTypeName == "GroupControl" and 
                        (child.Name or "").strip() == "sidebar_add"):
                        sidebar_add_group = child
                        found_depth = depth + 1
                        print(f"找到 'sidebar_add' 组 (深度: {depth + 1})")
                        return
                    find_sidebar_add(child, depth + 1, max_d)
            except:
                pass
        find_sidebar_add(container, 0, MAX_SEARCH_DEPTH)
        
        if not sidebar_add_group:
            print("未找到 'sidebar_add' 组")
            return created_names
        
        if not sidebar_add_group.Exists(3, INTERVAL):
            print("'sidebar_add' 组不可访问")
            return created_names
        
        # 2. 获取 sidebar_add 的父控件，然后找到下一个兄弟节点
        sidebar_add_parent = sidebar_add_group.GetParentControl()
        if not sidebar_add_parent:
            print("无法获取 'sidebar_add' 的父控件")
            return created_names
        
        siblings = sidebar_add_parent.GetChildren()
        if not siblings:
            print("父控件没有子组件")
            return created_names
        
        # 找到 sidebar_add 在兄弟节点中的位置
        sidebar_add_index = -1
        for i, sibling in enumerate(siblings):
            # 比较控件是否相同（使用 Handle 或其他属性）
            try:
                if hasattr(sibling, 'Handle') and hasattr(sidebar_add_group, 'Handle'):
                    if sibling.Handle == sidebar_add_group.Handle:
                        sidebar_add_index = i
                        break
                elif (sibling.ControlTypeName == sidebar_add_group.ControlTypeName and
                      (sibling.Name or "").strip() == (sidebar_add_group.Name or "").strip()):
                    sidebar_add_index = i
                    break
            except:
                pass
        
        if sidebar_add_index == -1:
            print("无法在兄弟节点中找到 'sidebar_add' 的位置")
            return created_names
        
        print(f"'sidebar_add' 在兄弟节点中的索引: {sidebar_add_index}, 总兄弟节点数: {len(siblings)}")
        
        # 3. 获取下一个兄弟节点（单个节点）
        if sidebar_add_index + 1 >= len(siblings):
            print("'sidebar_add' 没有下一个兄弟节点")
            return created_names
        
        next_sibling = siblings[sidebar_add_index + 1]
        print(f"找到 'sidebar_add' 的下一个兄弟节点: {next_sibling.ControlTypeName}, Name: {next_sibling.Name or 'N/A'}")
        
        if not next_sibling.Exists(3, INTERVAL):
            print("下一个兄弟节点不可访问")
            return created_names
        
        # 4. 在这个兄弟节点内部，使用滚动+点击刷新的方式提取歌单名称
        # 先点击"我喜欢的音乐"或任意一个歌单来激活列表
        try:
            # 尝试点击"我喜欢的音乐"来激活列表
            all_likes_control_obj = container.Control(AutomationId="left_nav_myFavoriteMusic", searchDepth=MAX_SEARCH_DEPTH)
            if all_likes_control_obj.Exists(1, 0):
                depth = _find_control_depth(container, all_likes_control_obj, MAX_SEARCH_DEPTH)
                print(f"找到 'left_nav_myFavoriteMusic' 控件，深度: {depth}")
                all_likes_controls = [all_likes_control_obj]
            else:
                all_likes_controls = []
            if all_likes_controls:
                all_likes_controls[0].Click()
                # 等待激活：检测列表是否可滚动
                if not next_sibling.Exists(1, 0.1):
                    for _ in range(10):
                        if next_sibling.Exists(0.1, 0.1):
                            break
        except:
            # 如果点击"我喜欢的音乐"失败，尝试点击next_sibling中的第一个歌单
            try:
                # 使用 GetChildren 递归查找 GroupControl
                playlist_groups = []
                def collect_playlist_groups(ctrl, depth=0, max_d=5):
                    if depth > max_d:
                        return
                    try:
                        children = ctrl.GetChildren()
                        for child in children:
                            if child.ControlTypeName == "GroupControl":
                                playlist_name = (child.Name or "").strip()
                                if playlist_name:  # 只收集有名称的 GroupControl
                                    playlist_groups.append(child)
                            collect_playlist_groups(child, depth + 1, max_d)
                    except:
                        pass
                collect_playlist_groups(next_sibling, 0, 5)
                if playlist_groups:
                    playlist_groups[0].Click()
                    # 等待激活：检测列表是否可访问
                    if not next_sibling.Exists(1, 0.1):
                        for _ in range(10):
                            if next_sibling.Exists(0.1, 0.1):
                                break
            except:
                pass
        
        max_iterations = 50  # 增加迭代次数，因为需要滚动
        iteration = 0
        last_clicked_index = -1  # 记录上一次点击的歌单在已获取列表中的绝对位置索引
        
        while iteration < max_iterations:
            # 使用键盘向下键滚动（每次按5次）
            try:
                # for _ in range(5):
                next_sibling.SendKeys('{DOWN 5}')
                # 等待滚动完成：检测列表内容是否变化
                before_visible = _extract_playlist_names_from_group(next_sibling)
                for _ in range(10):  # 最多等待1秒
                    after_visible = _extract_playlist_names_from_group(next_sibling)
                    if after_visible != before_visible:
                        break
            except Exception as e:
                print(f"键盘滚动创建歌单时出错: {e}")
                break
            
            # 每次滚动后都进行点击（点击上一次点击的后面第 8 个，基于已获取列表的绝对位置）
            print("滚动后点击上一次点击的歌单后面的第五个刷新...")
            try:
                # 收集所有可见的歌单控件
                playlist_groups = []
                def collect_playlist_groups(ctrl, depth=0, max_d=5):
                    if depth > max_d:
                        return
                    try:
                        children = ctrl.GetChildren()
                        for child in children:
                            if child.ControlTypeName == "GroupControl":
                                child_name = (child.Name or "").strip()
                                if child_name:  # 只收集有名称的 GroupControl
                                    playlist_groups.append(child)
                            collect_playlist_groups(child, depth + 1, max_d)
                    except:
                        pass
                collect_playlist_groups(next_sibling, 0, 5)
                
                if not playlist_groups:
                    print("未找到任何歌单控件，退出")
                    break
                
                target_playlist = None
                if last_clicked_index >= 0:
                    # 基于整个列表的绝对位置，计算目标位置
                    target_index = last_clicked_index + 8
                    if target_index < len(all_playlist_names):
                        target_name = all_playlist_names[target_index]
                        # 在当前可见的控件中查找对应名称的控件
                        for group in playlist_groups:
                            group_name = (group.Name or "").strip()
                            if group_name:
                                clean_group_name = _remove_special_chars(group_name)
                                if clean_group_name == target_name:
                                    target_playlist = group
                                    break
                else:
                    # 第一次点击，点击第一个歌单
                    if playlist_groups:
                        target_playlist = playlist_groups[0]
                
                if target_playlist:
                    # 点击目标歌单
                    target_playlist.Click()
                    target_name = (target_playlist.Name or "").strip()
                    clean_target_name = _remove_special_chars(target_name)
                    # 更新记录：找到点击的歌单在整个列表中的绝对位置
                    if clean_target_name in all_playlist_names:
                        last_clicked_index = all_playlist_names.index(clean_target_name)
                    else:
                        # 如果还没在列表中，使用当前整个列表长度作为位置（会在后续添加到列表）
                        last_clicked_index = len(all_playlist_names)
                    print(f"已点击歌单 (整个列表绝对位置: {last_clicked_index}): {target_name}")
                    
                    # 再滚动一次（5次down）
                    try:
                        # for _ in range(5):
                        next_sibling.SendKeys('{DOWN 5}')
                        # 等待滚动完成
                        before_visible = _extract_playlist_names_from_group(next_sibling)
                        for _ in range(10):
                            after_visible = _extract_playlist_names_from_group(next_sibling)
                            if after_visible != before_visible:
                                break
                    except Exception as e:
                        print(f"点击后滚动时出错: {e}")
                else:
                    print(f"无法找到目标歌单 (目标绝对位置: {last_clicked_index + 5 if last_clicked_index >= 0 else 0})，退出")
                    break
            except Exception as e:
                print(f"点击刷新创建歌单时出错: {e}")
                break
            
            # 提取当前可见的歌单名称并检查是否有新名称
            current_names = _extract_playlist_names_from_group(next_sibling)
            before_count = len(created_names)
            
            for name in current_names:
                clean_name = _remove_special_chars(name).strip()
                # 检查是否在类别内已存在，以及是否在整个列表和已存在集合中已存在
                if clean_name and clean_name not in created_names_set and clean_name not in existing_names_set:
                    created_names.append(clean_name)
                    created_names_set.add(clean_name)
                    existing_names_set.add(clean_name)  # 同步更新已存在集合
                    # 同步更新整个列表，确保索引计算准确
                    if clean_name not in all_playlist_names:
                        all_playlist_names.append(clean_name)
            
            after_count = len(created_names)
            
            # 检查是否有新名称，如果没有新名称，判定为彻底无刷新，退出
            if after_count <= before_count:
                print("点击后仍没有新名称，判定为歌单名称全部获取完毕")
                break
            else:
                print(f"点击后获取到 {after_count - before_count} 个新名称，继续...")
            
            iteration += 1
        
        if created_names:
            print(f"共找到 {len(created_names)} 个创建的歌单: {created_names[:5]}{'...' if len(created_names) > 5 else ''}")
        else:
            print("未找到创建的歌单（下一个兄弟节点中没有 GroupControl 或 Name 为空）")
    
    except Exception as e:
        print(f"获取创建的歌单时出错: {e}")
        import traceback
        traceback.print_exc()
    
    return created_names


def _extract_playlist_names_from_group(group):
    """
    从组中提取歌单名称（同部分内去重）
    
    Args:
        group: GroupControl控件
    
    Returns:
        list: 歌单名称列表（已去重）
    """
    playlist_names = []
    playlist_names_set = set()  # 用于同部分内去重
    
    try:
        # 在组中搜索所有可能的歌单名称控件
        # 方法1: 查找TextControl
        text_controls = []
        found_depths = []
        def collect_text_controls(ctrl, depth=0, max_d=5):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "TextControl":
                        text_controls.append(child)
                        found_depths.append(depth + 1)
                        if len(text_controls) <= 5:  # 只输出前5个的深度信息
                            print(f"找到 TextControl (深度: {depth + 1}, 名称: {child.Name or 'N/A'})")
                    collect_text_controls(child, depth + 1, max_d)
            except:
                pass
        collect_text_controls(group, 0, 5)
        if text_controls:
            print(f"共找到 {len(text_controls)} 个 TextControl，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
        for text_control in text_controls:
            name = text_control.Name
            if name and name.strip():
                clean_name = _remove_special_chars(name.strip())
                if clean_name and _is_playlist_name(clean_name) and clean_name not in playlist_names_set:
                    playlist_names.append(clean_name)
                    playlist_names_set.add(clean_name)
        
        # 方法2: 查找GroupControl中的名称（可能是嵌套的）
        sub_groups = []
        def collect_sub_groups(ctrl, depth=0, max_d=3):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "GroupControl":
                        sub_groups.append(child)
                    collect_sub_groups(child, depth + 1, max_d)
            except:
                pass
        collect_sub_groups(group, 0, 3)
        for sub_group in sub_groups:
            group_name = sub_group.Name
            if group_name and group_name.strip():
                clean_name = _remove_special_chars(group_name.strip())
                if clean_name and _is_playlist_name(clean_name) and clean_name not in playlist_names_set:
                    playlist_names.append(clean_name)
                    playlist_names_set.add(clean_name)
        
        # 方法3: 查找ListItemControl
        list_items = []
        def collect_list_items(ctrl, depth=0, max_d=5):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ListItemControl":
                        list_items.append(child)
                    collect_list_items(child, depth + 1, max_d)
            except:
                pass
        collect_list_items(group, 0, 5)
        for item in list_items:
            name = item.Name
            if name and name.strip():
                clean_name = _remove_special_chars(name.strip())
                if clean_name and _is_playlist_name(clean_name) and clean_name not in playlist_names_set:
                    playlist_names.append(clean_name)
                    playlist_names_set.add(clean_name)
    
    except Exception as e:
        print(f"从组中提取歌单名称时出错: {e}")
    
    return playlist_names


def _find_most_similar_playlist_name(target_name, playlist_names):
    """
    使用大模型API判断歌单列表中与目标名称最相似的名称
    
    Args:
        target_name: 目标歌单名称
        playlist_names: 歌单名称列表
    
    Returns:
        str: 最相似的歌单名称，如果找不到则返回None
    """
    if not LLM_API_AVAILABLE:
        # 如果大模型API不可用，使用简单的字符串匹配
        clean_target = _remove_special_chars(target_name).strip().lower()
        for name in playlist_names:
            clean_name = _remove_special_chars(name).strip().lower()
            if clean_target in clean_name or clean_name in clean_target:
                return name
        return None
    
    if not playlist_names:
        return None
    
    try:
        api_key = require_env("DEEPSEEK_THINKING_API_KEY", DEEPSEEK_THINKING_API_KEY)
        api_url = DEEPSEEK_API_URL
        model = DEEPSEEK_MODEL
        timeout = DEEPSEEK_API_TIMEOUT
        
        # 构建提示词
        playlist_list_str = "\n".join([f"{i+1}. {name}" for i, name in enumerate(playlist_names)])
        
        prompt = f"""你是一个智能助手，需要从给定的歌单名称列表中找到与目标名称最相似的一个。

歌单名称列表：
{playlist_list_str}

目标名称：{target_name}

请仔细比较目标名称与列表中的每个名称，找出最相似的一个。
只返回最相似的歌单名称（完全按照列表中的格式），如果列表中没有相似的名称，则只返回 "None"。
不要返回任何其他内容，只返回歌单名称或 "None"。

示例：
如果目标名称是"我的歌单"，列表中有"我的音乐歌单"，则返回"我的音乐歌单"。
如果目标名称是"周杰伦"，列表中有"周杰伦精选"，则返回"周杰伦精选"。
如果目标名称是"完全不相关的名称"，列表中没有任何相似的，则返回"None"。
"""
        
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.3,  # 较低的温度以获得更确定的结果
            "max_tokens": 100,
            "stream": False
        }
        
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
        
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=timeout
        )
        response.raise_for_status()
        result = response.json()
        
        # 提取返回的内容
        content = result["choices"][0]["message"]["content"].strip()
        
        # 处理返回结果
        if content.lower() == "none" or content.lower() == "null":
            return None
        
        # 检查返回的名称是否在列表中
        for name in playlist_names:
            if content == name or content in name or name in content:
                return name
        
        # 如果返回的内容不在列表中，尝试模糊匹配
        clean_content = _remove_special_chars(content).strip().lower()
        clean_target = _remove_special_chars(target_name).strip().lower()
        
        for name in playlist_names:
            clean_name = _remove_special_chars(name).strip().lower()
            if clean_content == clean_name or clean_target in clean_name or clean_name in clean_target:
                return name
        
        return None
        
    except Exception as e:
        print(f"调用大模型API进行名称匹配时出错: {e}")
        # 出错时回退到简单匹配
        clean_target = _remove_special_chars(target_name).strip().lower()
        for name in playlist_names:
            clean_name = _remove_special_chars(name).strip().lower()
            if clean_target in clean_name or clean_name in clean_target:
                return name
        return None


def _contains_text(control, text):
    """
    检查控件或其子控件是否包含指定文本
    
    Args:
        control: uiautomation控件
        text: 要查找的文本
    
    Returns:
        bool: 是否包含该文本
    """
    try:
        if text in (control.Name or ""):
            return True
        # 递归检查子控件
        children = control.GetChildren()
        for child in children:
            if _contains_text(child, text):
                return True
    except:
        pass
    return False


def _find_playlist_container(playlist_group):
    """
    找到包含歌单列表的容器（通常是包含"我喜欢的音乐"的列表容器）
    
    Args:
        playlist_group: 包含"我喜欢的音乐"的组
    
    Returns:
        列表容器控件，如果找不到则返回None
    """
    try:
        # 尝试向上查找列表容器
        current = playlist_group
        for _ in range(5):  # 最多向上查找5层
            parent = current.GetParentControl()
            if not parent:
                break
            
            # 检查是否是列表容器（ListControl或包含多个子项的GroupControl）
            if parent.ControlTypeName in ["ListControl", "GroupControl"]:
                # 检查是否包含多个子项（可能是歌单列表）
                try:
                    children = parent.GetChildren()
                    if len(children) > 1:
                        return parent
                except:
                    pass
            
            current = parent
        
        # 如果找不到，返回playlist_group本身
        return playlist_group
    except:
        return playlist_group


def _relocate_all_likes():
    """
    重新定位ALL_LIKES容器
    
    Returns:
        重新定位的ALL_LIKES控件（容器），如果找不到则返回None
    """
    global ALL_LIKES
    try:
        if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
            return None
        
        # 通过AutomationId重新定位容器
        # 使用 FindAll 查找 AutomationId 为 "left_nav_myFavoriteMusic" 的控件
        control_obj = MAIN_WINDOW.Control(AutomationId="left_nav_myFavoriteMusic", searchDepth=MAX_SEARCH_DEPTH)
        controls = [control_obj] if control_obj.Exists(1, 0) else []
        if controls and len(controls) > 0:
            control = controls[0]
            parent = control.GetParentControl()  # 父组件
            if parent:
                ALL_LIKES = parent.GetParentControl()  # 父组件的父组件（容器）
                return ALL_LIKES
    except Exception as e:
        print(f"重新定位ALL_LIKES时出错: {e}")
    
    return None


def _locate_all_likes():
    """
    定位ALL_LIKES容器（用于switch_to_playlist等函数）
    
    Returns:
        ALL_LIKES控件（容器），如果找不到则返回None
    """
    try:
        if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
            return None
        
        # 通过AutomationId定位容器
        # 使用 FindAll 查找 AutomationId 为 "left_nav_myFavoriteMusic" 的控件
        control_obj = MAIN_WINDOW.Control(AutomationId="left_nav_myFavoriteMusic", searchDepth=MAX_SEARCH_DEPTH)
        controls = [control_obj] if control_obj.Exists(1, 0) else []
        if controls and len(controls) > 0:
            control = controls[0]
            parent = control.GetParentControl()  # 父组件
            if parent:
                return parent.GetParentControl()  # 父组件的父组件（容器）
    except Exception as e:
        print(f"定位ALL_LIKES时出错: {e}")
    
    return None


def _get_visible_playlists():
    """
    获取当前容器中可见的歌单名称列表（按从上到下的顺序）
    
    Returns:
        list: 当前可见的歌单名称列表
    """
    visible_playlists = []
    try:
        global ALL_LIKES
        if not ALL_LIKES or not ALL_LIKES.Exists(0.1, 0.1):
            return visible_playlists
        
        # 在容器中搜索所有可能的歌单控件
        try:
            # 在容器中搜索TextControl
            text_controls = []
            found_depths = []
            def collect_text_controls(ctrl, depth=0, max_d=10):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName == "TextControl":
                            text_controls.append(child)
                            found_depths.append(depth + 1)
                        collect_text_controls(child, depth + 1, max_d)
                except:
                    pass
            collect_text_controls(ALL_LIKES, 0, 10)
            if text_controls:
                print(f"找到 {len(text_controls)} 个 TextControl，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
            for text_control in text_controls:
                name = text_control.Name
                if name and name.strip():
                    clean_name = _remove_special_chars(name.strip())
                    if clean_name and _is_playlist_name(clean_name):
                        visible_playlists.append(clean_name)
            
            # 在容器中搜索GroupControl
            groups = []
            found_depths = []
            def collect_groups(ctrl, depth=0, max_d=10):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName == "GroupControl":
                            groups.append(child)
                            found_depths.append(depth + 1)
                        collect_groups(child, depth + 1, max_d)
                except:
                    pass
            collect_groups(ALL_LIKES, 0, 10)
            if groups:
                print(f"找到 {len(groups)} 个 GroupControl，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
            for group in groups:
                name = group.Name
                if name and name.strip():
                    clean_name = _remove_special_chars(name.strip())
                    if clean_name and _is_playlist_name(clean_name):
                        visible_playlists.append(clean_name)
            
            # 在容器中搜索ListItemControl
            list_items = []
            found_depths = []
            def collect_list_items(ctrl, depth=0, max_d=10):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName == "ListItemControl":
                            list_items.append(child)
                            found_depths.append(depth + 1)
                        collect_list_items(child, depth + 1, max_d)
                except:
                    pass
            collect_list_items(ALL_LIKES, 0, 10)
            if list_items:
                print(f"找到 {len(list_items)} 个 ListItemControl，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
            for item in list_items:
                name = item.Name
                if name and name.strip():
                    clean_name = _remove_special_chars(name.strip())
                    if clean_name and _is_playlist_name(clean_name):
                        visible_playlists.append(clean_name)
        except Exception as e:
            print(f"使用控件方法获取可见歌单时出错: {e}")
    except Exception as e:
        print(f"获取可见歌单时出错: {e}")
    
    return visible_playlists


def _sync_anchor_with_visible(all_playlist_names):
    """
    根据当前容器中可见的歌单，同步更新 PLAYLIST_ANCHOR
    这是一个闭环控制：基于实际可见内容来更新索引，而不是假设滚动距离
    
    Args:
        all_playlist_names: 完整的歌单名称列表
    
    Returns:
        bool: 是否成功同步了锚点
    """
    global PLAYLIST_ANCHOR
    
    visible_playlists = _get_visible_playlists()
    if not visible_playlists:
        return False
    
    # 尝试在完整列表中找到可见歌单的位置
    # 策略：找到第一个可见歌单在完整列表中的索引
    for visible_name in visible_playlists:
        for i, full_name in enumerate(all_playlist_names):
            # 使用模糊匹配
            clean_visible = _remove_special_chars(visible_name).strip().lower()
            clean_full = _remove_special_chars(full_name).strip().lower()
            
            if clean_visible in clean_full or clean_full in clean_visible or clean_visible == clean_full:
                # 找到匹配，更新锚点为该索引
                PLAYLIST_ANCHOR = i
                return True
    
    return False


def _search_group_by_name(name, container=None):
    """
    在容器中搜索包含指定名称的组
    
    Args:
        name: 要搜索的名称（可以是部分匹配）
        container: 容器控件，如果为None则使用ALL_LIKES或MAIN_WINDOW
    
    Returns:
        找到的组控件，如果找不到则返回None
    """
    try:
        # 清理名称（去除特殊字符）
        clean_name = _remove_special_chars(name)
        if not clean_name:
            return None
        
        # 确定搜索范围
        search_container = container
        if not search_container:
            global ALL_LIKES
            search_container = ALL_LIKES if ALL_LIKES else MAIN_WINDOW
        
        if not search_container or not search_container.Exists(0.1, 0.1):
            return None
        
        # 在容器中搜索包含该名称的控件
        # 优先搜索GroupControl
        groups = []
        def collect_groups(ctrl, depth=0, max_d=10):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "GroupControl":
                        groups.append(child)
                    collect_groups(child, depth + 1, max_d)
            except:
                pass
        collect_groups(search_container, 0, 10)
        for group in groups:
            group_name = group.Name or ""
            clean_group_name = _remove_special_chars(group_name)
            # 检查是否包含目标名称（部分匹配）
            if clean_name in clean_group_name or clean_group_name in clean_name:
                # 进一步检查：确保这是一个歌单组（不是其他类型的组）
                if _is_playlist_name(clean_group_name):
                    return group
        
        # 也搜索其他类型的控件（如ListItemControl、ButtonControl等）
        all_controls = []
        def collect_all_controls(ctrl, depth=0, max_d=10):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    all_controls.append(child)
                    collect_all_controls(child, depth + 1, max_d)
            except:
                pass
        collect_all_controls(search_container, 0, 10)
        for control in all_controls:
            control_name = control.Name or ""
            clean_control_name = _remove_special_chars(control_name)
            if clean_name in clean_control_name or clean_control_name in clean_name:
                if _is_playlist_name(clean_control_name):
                    return control
    except Exception as e:
        print(f"搜索组时出错: {e}")
    
    return None


def _extract_playlist_names_from_window():
    """
    从窗口提取歌单名称（不依赖特定容器，用于滚动时容器可能不可见的情况）
    
    Returns:
        list: 歌单名称列表
    """
    playlist_names = []
    
    try:
        global ALL_LIKES
        # 优先在容器中搜索
        if ALL_LIKES:
            playlist_names = _extract_playlist_names(ALL_LIKES)
            if playlist_names:
                return playlist_names
        
        # 备用方法：在MAIN_WINDOW中搜索所有可能的歌单控件
        text_controls = []
        def collect_text_controls(ctrl, depth=0, max_d=10):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "TextControl":
                        text_controls.append(child)
                    collect_text_controls(child, depth + 1, max_d)
            except:
                pass
        collect_text_controls(MAIN_WINDOW, 0, 10)
        for text_control in text_controls:
            name = text_control.Name
            if name and name.strip():
                clean_name = _remove_special_chars(name.strip())
                if clean_name and _is_playlist_name(clean_name):
                    playlist_names.append(clean_name)
        
        list_items = []
        def collect_list_items(ctrl, depth=0, max_d=10):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ListItemControl":
                        list_items.append(child)
                    collect_list_items(child, depth + 1, max_d)
            except:
                pass
        collect_list_items(MAIN_WINDOW, 0, 10)
        for item in list_items:
            name = item.Name
            if name and name.strip():
                clean_name = _remove_special_chars(name.strip())
                if clean_name and _is_playlist_name(clean_name):
                    playlist_names.append(clean_name)
    except Exception as e:
        print(f"使用控件方法提取歌单名称时出错: {e}")
    
    return playlist_names


def _extract_playlist_names(container):
    """
    从容器中提取歌单名称
    
    Args:
        container: uiautomation控件容器
    
    Returns:
        list: 歌单名称列表
    """
    playlist_names = []
    
    try:
        # 方法1: 查找所有TextControl，过滤出可能的歌单名称
        text_controls = []
        def collect_text_controls(ctrl, depth=0, max_d=5):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "TextControl":
                        text_controls.append(child)
                    collect_text_controls(child, depth + 1, max_d)
            except:
                pass
        collect_text_controls(container, 0, 5)
        for text_control in text_controls:
            name = text_control.Name
            if name and name.strip():
                # 去除特殊字符
                clean_name = _remove_special_chars(name.strip())
                if clean_name and _is_playlist_name(clean_name):
                    playlist_names.append(clean_name)
        
        # 方法2: 查找ListItemControl（如果歌单是列表项）
        list_items = []
        def collect_list_items(ctrl, depth=0, max_d=5):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ListItemControl":
                        list_items.append(child)
                    collect_list_items(child, depth + 1, max_d)
            except:
                pass
        collect_list_items(container, 0, 5)
        for item in list_items:
            name = item.Name
            if name and name.strip():
                clean_name = _remove_special_chars(name.strip())
                if clean_name and _is_playlist_name(clean_name):
                    playlist_names.append(clean_name)
        
        # 方法3: 查找ButtonControl（如果歌单是按钮形式）
        buttons = []
        def collect_buttons(ctrl, depth=0, max_d=5):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ButtonControl":
                        buttons.append(child)
                    collect_buttons(child, depth + 1, max_d)
            except:
                pass
        collect_buttons(container, 0, 5)
        for button in buttons:
            name = button.Name
            if name and name.strip():
                clean_name = _remove_special_chars(name.strip())
                if clean_name and _is_playlist_name(clean_name):
                    playlist_names.append(clean_name)
    except Exception as e:
        print(f"使用控件方法提取歌单名称时出错: {e}")
    
    return playlist_names


def _remove_special_chars(text):
    """
    去除特殊字符，只保留纯文本（中文、英文、数字、空格）
    
    Args:
        text: 原始文本
    
    Returns:
        str: 去除特殊字符后的纯文本
    """
    if not text:
        return ""
    
    # 使用正则表达式，只保留中文、英文、数字、空格
    # \u4e00-\u9fff 是中文字符范围
    # a-zA-Z0-9 是英文和数字
    # \s 是空白字符（空格、制表符等）
    pattern = r'[^\u4e00-\u9fff\w\s]'
    clean_text = re.sub(pattern, '', text)
    
    # 去除多余的空格
    clean_text = ' '.join(clean_text.split())
    
    return clean_text.strip()




def _is_playlist_name(text):
    """
    判断文本是否可能是歌单名称
    
    Args:
        text: 文本内容
    
    Returns:
        bool: 是否可能是歌单名称
    """
    if not text or len(text.strip()) == 0:
        return False
    
    # 过滤掉一些明显不是歌单名称的文本（按钮、操作等）
    # 注意："我喜欢的音乐"本身是歌单名称，不应该被排除
    exclude_keywords = [
        "播放", "暂停", "下一首", "上一首", "音量", "搜索", "单曲", "专辑", "歌手",
        "创建", "删除", "编辑", "分享", "收藏", "下载", "更多", "设置", "帮助"
    ]
    
    text_lower = text.lower()
    for keyword in exclude_keywords:
        # 如果文本就是这些关键词本身，则排除
        if text.strip() == keyword:
            return False
    
    # 过滤掉太短的文本（可能是图标或按钮标签）
    if len(text.strip()) < 2:
        return False
    
    # 过滤掉纯数字或特殊字符
    if text.strip().isdigit():
        return False
    
    return True

@require_uia
def switch_to_playlist(playlist_name=None):
    """
    切换到指定歌单
    
    使用索引定位方法：
    1. 先调用get_playlist_name_in_netease获取全部歌单名称（第一个应该是ALL_LIKES）
    2. 如果表中存在与传入参数相同的名称则使用该名称，如果不存在则拒绝执行
    3. 先尝试搜索并定位包含该名称的组
    4. 若定位失败，则尝试定位列表中第2个名称组
    5. 若定位失败，则尝试定位ALL_LIKES
    6. 随后进行滚动，每向下滚动一次尝试定位PLAYLIST_ANCHOR+1索引位置的名称组（向上滚动则是PLAYLIST_ANCHOR-1）
    7. 定位成功则更新PLAYLIST_ANCHOR并继续定位下一个名称组（不滚动），若定位失败再尝试下一次滚动定位循环
    8. 若PLAYLIST_ANCHOR不为0，检查PLAYLIST_ANCHOR的索引位置与目标名称的索引位置关系，若小于目标名称索引则向下滚动，若大于目标名称则向上滚动
    
    Args:
        playlist_name (str, optional): 歌单名称。如果提供，会先打开该歌单
    
    Returns:
        str: 操作结果描述
    """
    if not playlist_name:
        return "错误: 未提供歌单名称"
    
    _ensure_netease_window_active()
    dropdown()
    
    if not MAIN_WINDOW.Exists(3, INTERVAL):
        return "错误: 找不到网易云音乐主窗口"
    
    # 在进行原有逻辑之前，点击窗口左侧边界线中心点旁的位置，然后发送HOME键以正确识别锚点
    _click_left_edge_and_home()
    
    global PLAYLIST_ANCHOR, ALL_LIKES, PLAY_LIST
    
    # 1. 获取全部歌单名称（使用缓存如果存在）
    if PLAY_LIST:
        all_playlist_names = PLAY_LIST
    else:
        all_playlist_names = get_playlist_name_in_netease()
    
    if not all_playlist_names:
        return "错误: 无法获取歌单列表"
    
    # 2. 使用大模型API判断列表中是否存在与目标最相似的名称
    matched_name = _find_most_similar_playlist_name(playlist_name, all_playlist_names)
    
    if matched_name is None:
        # 如果找不到匹配的歌单，尝试强制刷新缓存后再次查找
        all_playlist_names = get_playlist_name_in_netease(force_refresh=True)
        if not all_playlist_names:
            return "错误: 无法获取歌单列表"
        matched_name = _find_most_similar_playlist_name(playlist_name, all_playlist_names)
        if matched_name is None:
            return f"错误: 歌单列表中不存在与 '{playlist_name}' 相似的名称，拒绝执行。可用歌单列表：{', '.join(all_playlist_names[:10])}{'...' if len(all_playlist_names) > 10 else ''}"
    
    # 使用匹配到的名称进行后续操作
    actual_playlist_name = matched_name
    
    # 确保ALL_LIKES（容器）已初始化
    if not ALL_LIKES:
        # 通过"我喜欢的音乐"组找到容器
        try:
            if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
                return "错误: 找不到网易云音乐主窗口"
            
            # 使用 FindAll 查找 AutomationId 为 "left_nav_myFavoriteMusic" 的控件
            control_obj = MAIN_WINDOW.Control(AutomationId="left_nav_myFavoriteMusic", searchDepth=MAX_SEARCH_DEPTH)
            if control_obj.Exists(1, 0):
                depth = _find_control_depth(MAIN_WINDOW, control_obj, MAX_SEARCH_DEPTH)
                print(f"找到 'left_nav_myFavoriteMusic' 控件，深度: {depth}")
                controls = [control_obj]
            else:
                controls = []
            if controls and len(controls) > 0:
                control = controls[0]
                parent = control.GetParentControl()  # 父组件
                if parent:
                    ALL_LIKES = parent.GetParentControl()  # 父组件的父组件（容器）
        except Exception as e:
            print(f"定位容器时出错: {e}")
        
        if not ALL_LIKES:
            return "错误: 无法定位容器"
    
    # 3. 找到匹配名称在列表中的索引
    target_index = None
    for i, name in enumerate(all_playlist_names):
        if name == actual_playlist_name:
            target_index = i
            break
    
    if target_index is None:
        return f"错误: 无法在列表中定位匹配到的歌单名称 '{actual_playlist_name}'"
    
    # 3. 先尝试搜索并定位包含该名称的组（在容器中搜索）
    target_group = _search_group_by_name(actual_playlist_name, ALL_LIKES)
    if target_group:
        try:
            target_group.Click()
            # 等待歌单切换：检测搜索框是否出现（歌单打开后会有搜索框）
            search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
            if not search_box.Exists(3, 0.1):
                # 等待搜索框出现
                for _ in range(20):  # 最多等待2秒
                    search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                    if search_box.Exists(0.1, 0.1):
                        break
            PLAYLIST_ANCHOR = target_index  # 更新锚点
            return f"已切换到歌单: {actual_playlist_name}"
        except Exception as e:
            print(f"点击目标组时出错: {e}")
    
    # 4. 若定位失败，尝试定位列表中第2个名称组（索引1）
    if len(all_playlist_names) > 1:
        second_name = all_playlist_names[1]
        second_group = _search_group_by_name(second_name, ALL_LIKES)
        if second_group:
            try:
                second_group.Click()
                # 等待点击生效：检测目标组是否可见
                target_group = None
                for _ in range(10):  # 最多等待1秒
                    target_group = _search_group_by_name(actual_playlist_name, ALL_LIKES)
                    if target_group and target_group.Exists(0.1, 0.1):
                        break
                PLAYLIST_ANCHOR = 1  # 更新锚点
                # 继续定位目标（不滚动）
                if target_group:
                    target_group.Click()
                    # 等待歌单切换：检测搜索框是否出现
                    search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                    if not search_box.Exists(3, 0.1):
                        for _ in range(20):
                            search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                            if search_box.Exists(0.1, 0.1):
                                break
                    PLAYLIST_ANCHOR = target_index
                    return f"已切换到歌单: {actual_playlist_name}"
            except Exception as e:
                print(f"点击第2个名称组时出错: {e}")
    
    # 5. 若定位失败，尝试定位ALL_LIKES（索引0）
    all_likes_group = _search_group_by_name("我喜欢的音乐", ALL_LIKES)
    if all_likes_group:
        try:
            all_likes_group.Click()
            # 等待点击生效：检测目标组是否可见
            target_group = None
            for _ in range(10):  # 最多等待1秒
                target_group = _search_group_by_name(actual_playlist_name, ALL_LIKES)
                if target_group and target_group.Exists(0.1, 0.1):
                    break
            PLAYLIST_ANCHOR = 0  # 更新锚点
            # 继续定位目标（不滚动）
            if target_group:
                target_group.Click()
                # 等待歌单切换：检测搜索框是否出现
                search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                if not search_box.Exists(3, 0.1):
                    for _ in range(20):
                        search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                        if search_box.Exists(0.1, 0.1):
                            depth = _find_control_depth(MAIN_WINDOW, search_box, MAX_SEARCH_DEPTH)
                            print(f"找到搜索框，深度: {depth}")
                            break
                else:
                    depth = _find_control_depth(MAIN_WINDOW, search_box, MAX_SEARCH_DEPTH)
                    print(f"找到搜索框，深度: {depth}")
                PLAYLIST_ANCHOR = target_index
                return f"已切换到歌单: {actual_playlist_name}"
        except Exception as e:
            print(f"点击ALL_LIKES时出错: {e}")
    
    # 6. 使用闭环控制：基于实际可见内容来定位目标
    # 首先尝试同步当前锚点与实际可见内容
    _sync_anchor_with_visible(all_playlist_names)
    
    # 检查目标是否已经在可见列表中
    visible_playlists = _get_visible_playlists()
    for visible_name in visible_playlists:
        clean_visible = _remove_special_chars(visible_name).strip().lower()
        clean_target = _remove_special_chars(actual_playlist_name).strip().lower()
        if clean_visible in clean_target or clean_target in clean_visible or clean_visible == clean_target:
            # 目标已在可见列表中，直接点击
            target_group = _search_group_by_name(actual_playlist_name, ALL_LIKES)
            if target_group:
                try:
                    target_group.Click()
                    # 等待歌单切换：检测搜索框是否出现
                    search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                    if not search_box.Exists(3, 0.1):
                        for _ in range(20):
                            search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                            if search_box.Exists(0.1, 0.1):
                                break
                    # 更新锚点
                    for i, name in enumerate(all_playlist_names):
                        if name == actual_playlist_name:
                            PLAYLIST_ANCHOR = i
                            break
                    return f"已切换到歌单: {actual_playlist_name}"
                except Exception as e:
                    print(f"点击可见目标时出错: {e}")
    
    # 7. 如果目标不在可见列表中，使用键盘滚动定位
    # 先激活列表（点击"我喜欢的音乐"或任意一个歌单）
    try:
        # 尝试点击"我喜欢的音乐"来激活列表
        all_likes_control_obj = ALL_LIKES.Control(AutomationId="left_nav_myFavoriteMusic", searchDepth=MAX_SEARCH_DEPTH)
        if all_likes_control_obj.Exists(1, 0):
            depth = _find_control_depth(ALL_LIKES, all_likes_control_obj, MAX_SEARCH_DEPTH)
            print(f"找到 'left_nav_myFavoriteMusic' 控件，深度: {depth}")
            all_likes_controls = [all_likes_control_obj]
        else:
            all_likes_controls = []
        if all_likes_controls:
            all_likes_controls[0].Click()
            # 等待激活：检测列表是否可滚动
            if not ALL_LIKES.Exists(1, 0.1):
                for _ in range(10):
                    if ALL_LIKES.Exists(0.1, 0.1):
                        break
        else:
            # 如果找不到"我喜欢的音乐"，尝试点击第一个可见的歌单
            visible_groups = []
            def collect_groups(ctrl, depth=0, max_d=5):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName == "GroupControl":
                            visible_groups.append(child)
                        collect_groups(child, depth + 1, max_d)
                except:
                    pass
            collect_groups(ALL_LIKES, 0, 5)
            if visible_groups:
                visible_groups[0].Click()
                # 等待激活：检测列表是否可访问
                if not ALL_LIKES.Exists(1, 0.1):
                    for _ in range(10):
                        if ALL_LIKES.Exists(0.1, 0.1):
                            break
    except Exception as e:
        print(f"激活列表时出错: {e}")
    
    max_scrolls = 100  # 增加最大滚动次数，因为键盘滚动更精确
    scroll_count = 0
    consecutive_no_progress = 0
    max_no_progress = 5  # 连续5次没有进展则停止
    last_anchor = PLAYLIST_ANCHOR
    
    while scroll_count < max_scrolls and consecutive_no_progress < max_no_progress:
        # 闭环控制：每次滚动后重新识别可见内容并同步索引
        try:
            # 根据当前锚点和目标索引决定滚动方向
            if PLAYLIST_ANCHOR < target_index:
                # 需要向下滚动
                scroll_direction = "down"
            elif PLAYLIST_ANCHOR > target_index:
                # 需要向上滚动
                scroll_direction = "up"
            else:
                # 已经在目标位置，但可能不在可见区域，尝试直接定位
                target_group = _search_group_by_name(actual_playlist_name, ALL_LIKES)
                if target_group:
                        try:
                            target_group.Click()
                            # 等待歌单切换：检测搜索框是否出现
                            search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                            if not search_box.Exists(3, 0.1):
                                for _ in range(20):
                                    search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                                    if search_box.Exists(0.1, 0.1):
                                        break
                            return f"已切换到歌单: {actual_playlist_name}"
                        except:
                            pass
                # 如果点击失败，继续滚动
                scroll_direction = "down"
            
            # 使用键盘上下键滚动（每次按5次）
            try:
                if scroll_direction == "down":
                    for _ in range(5):
                        ALL_LIKES.SendKeys('{DOWN}')
                else:
                    for _ in range(5):
                        ALL_LIKES.SendKeys('{UP}')
            except Exception as e:
                print(f"键盘滚动时出错: {e}")
                break
            
            # 等待滚动完成：检测可见内容是否变化
            before_visible = _get_visible_playlists()
            for _ in range(10):  # 最多等待1秒
                after_visible = _get_visible_playlists()
                if after_visible != before_visible:
                    break
            
            # 检查ALL_LIKES是否仍然可见
            try:
                if not ALL_LIKES or not ALL_LIKES.Exists(0.1, 0.1):
                    ALL_LIKES = _relocate_all_likes()
                    if not ALL_LIKES:
                        break
            except:
                pass
            
            # 闭环控制：重新识别可见内容并同步索引
            if _sync_anchor_with_visible(all_playlist_names):
                # 成功同步，检查是否有进展
                if PLAYLIST_ANCHOR != last_anchor:
                    consecutive_no_progress = 0  # 有进展，重置计数器
                    last_anchor = PLAYLIST_ANCHOR
                else:
                    consecutive_no_progress += 1
            else:
                # 同步失败，可能滚动过度或没有进展
                consecutive_no_progress += 1
            
            # 检查目标是否现在可见
            visible_playlists = _get_visible_playlists()
            for visible_name in visible_playlists:
                clean_visible = _remove_special_chars(visible_name).strip().lower()
                clean_target = _remove_special_chars(actual_playlist_name).strip().lower()
                if clean_visible in clean_target or clean_target in clean_visible or clean_visible == clean_target:
                    # 目标现在可见，尝试点击
                    target_group = _search_group_by_name(actual_playlist_name, ALL_LIKES)
                    if target_group:
                        try:
                            target_group.Click()
                            # 等待歌单切换：检测搜索框是否出现
                            search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                            if not search_box.Exists(3, 0.1):
                                for _ in range(20):
                                    search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                                    if search_box.Exists(0.1, 0.1):
                                        break
                            # 更新锚点
                            for i, name in enumerate(all_playlist_names):
                                if name == actual_playlist_name:
                                    PLAYLIST_ANCHOR = i
                                    break
                            return f"已切换到歌单: {actual_playlist_name}"
                        except Exception as e:
                            print(f"点击目标时出错: {e}")
            
            scroll_count += 1
            
        except Exception as e:
            print(f"键盘滚动时出错: {e}")
            consecutive_no_progress += 1
            scroll_count += 1
    
    return f"错误: 无法定位到歌单 '{actual_playlist_name}'（目标索引: {target_index}, 当前锚点: {PLAYLIST_ANCHOR}）"


@require_uia
def play_song_from_playlist(playlist_name=None, song_name=None, song_index=None):
    """
    从歌单中播放特定歌曲
    
    Args:
        playlist_name (str, optional): 歌单名称。如果提供，会先打开该歌单
        song_name (str, optional): 歌曲名称。如果提供，会在歌单中搜索并播放
        song_index (int, optional): 歌曲在歌单中的索引（从0开始）。如果提供，直接播放该索引的歌曲
    
    Returns:
        str: 操作结果描述
    """
    _ensure_netease_window_active()
    dropdown()
    switch_to_playlist(playlist_name)
    search_box_in_playlist = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
    if search_box_in_playlist.Exists(3,INTERVAL):
        depth = _find_control_depth(MAIN_WINDOW, search_box_in_playlist, MAX_SEARCH_DEPTH)
        print(f"找到搜索框控件，深度: {depth}")
        # 使用 ValuePattern 设置文本，如果没有则使用 SendKeys
        try:
            # 尝试使用 ValuePattern
            value_pattern = search_box_in_playlist.GetValuePattern()
            if value_pattern:
                value_pattern.SetValue(song_name)
                print(f"使用 ValuePattern 设置文本: {song_name}")
            else:
                raise AttributeError("ValuePattern not available")
        except (AttributeError, Exception) as e:
            # 如果 ValuePattern 失败，使用 SendKeys（先清空再输入）
            print(f"ValuePattern 不可用，使用 SendKeys: {e}")
            search_box_in_playlist.SendKeys('^a')  # Ctrl+A 全选
            time.sleep(0.1)  # 短暂等待
            search_box_in_playlist.SendKeys(song_name)
        # 不用回车，应用内置自动搜索功能
        # search_box_in_playlist.SendKeys('{ENTER}')
        # 等待搜索结果加载
        time.sleep(0.5)  # 等待搜索完成
        
        # 直接全局搜索"grid"表格
        try:
            grid_table = None
            for _ in range(20):  # 最多等待2秒
                # 全局搜索 Name="grid" 的 TableControl
                grid_table = MAIN_WINDOW.TableControl(Name='grid', searchDepth=MAX_SEARCH_DEPTH)
                if grid_table.Exists(0.1, 0.1):
                    depth = _find_control_depth(MAIN_WINDOW, grid_table, MAX_SEARCH_DEPTH)
                    print(f"找到 'grid' 表格，深度: {depth}")
                    break
                time.sleep(0.1)
            
            if not grid_table or not grid_table.Exists(3, INTERVAL):
                return f"失败: 搜索后无法找到 'grid' 表格"
            
            # 在表格内查找 Name="" 的行组（GroupControl）
            row_group = None
            row_groups = []
            found_depths = []
            def find_row_group(ctrl, depth=0, max_d=5):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if (child.ControlTypeName == "GroupControl" and 
                            (child.Name or "").strip() == ""):
                            row_groups.append(child)
                            found_depths.append(depth + 1)
                            if len(row_groups) <= 3:  # 只输出前3个的深度信息
                                print(f"找到行组 (深度: {depth + 1})")
                        find_row_group(child, depth + 1, max_d)
                except:
                    pass
            find_row_group(grid_table, 0, 5)
            
            if row_groups:
                print(f"共找到 {len(row_groups)} 个行组，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
                row_group = row_groups[0]  # 使用第一个行组
            else:
                return f"失败: 在 'grid' 表格中找不到空名称的行组"
            
            if not row_group or not row_group.Exists(3, INTERVAL):
                return f"失败: 行组不可访问"
            
            # 在该行组内查找子组（搜索结果）
            result_groups = []
            result_depths = []
            def find_result_groups(ctrl, depth=0, max_d=5):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName == "GroupControl":
                            result_groups.append(child)
                            result_depths.append(depth + 1)
                            if len(result_groups) <= 3:  # 只输出前3个的深度信息
                                print(f"找到搜索结果组 (深度: {depth + 1})")
                        find_result_groups(child, depth + 1, max_d)
                except:
                    pass
            find_result_groups(row_group, 0, 5)
            
            if result_groups:
                print(f"共找到 {len(result_groups)} 个搜索结果组，深度范围: {min(result_depths) if result_depths else 'N/A'} - {max(result_depths) if result_depths else 'N/A'}")
                # 点击第一个搜索结果
                first_result = result_groups[0]
                if first_result.Exists(3, INTERVAL):
                    try:
                        first_result.DoubleClick()
                        print(f"已点击第一个搜索结果")
                        return f"成功: 已播放歌曲 '{song_name}'"
                    except Exception as e:
                        return f"失败: 点击搜索结果时出错 - {str(e)}"
                else:
                    return f"失败: 第一个搜索结果不可访问"
            else:
                return f"失败: 搜索结果为空，未找到匹配的歌曲 '{song_name}'"
        except Exception as e:
            return f"失败: 查找搜索结果时出错 - {str(e)}"
    else:
        return f"失败: 找不到歌单内的搜索框"


@require_uia
def play_all_songs_in_playlist(playlist_name=None):
    """
    在指定歌单中播放全部歌曲
    
    Args:
        playlist_name (str, optional): 歌单名称。如果提供，会先切换到该歌单
    
    Returns:
        str: 操作结果描述
    """
    _ensure_netease_window_active()
    dropdown()
    
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        return "失败: 找不到网易云音乐主窗口"
    
    # 如果提供了歌单名称，先切换到该歌单
    if playlist_name:
        switch_result = switch_to_playlist(playlist_name)
        if switch_result.startswith("失败:") or switch_result.startswith("错误:"):
            return f"失败: 无法切换到歌单 '{playlist_name}' - {switch_result}"
        # 等待歌单切换完成：检测播放全部按钮是否出现
        play_all_button_check = None
        for _ in range(20):  # 最多等待2秒
            # 直接查找包含"播放全部"的按钮
            play_all_button_obj = MAIN_WINDOW.ButtonControl(searchDepth=MAX_SEARCH_DEPTH)
            play_all_buttons_check = []
            def find_play_all_buttons(ctrl, depth=0, max_d=20):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName == "ButtonControl" and "播放全部" in (child.Name or ""):
                            play_all_buttons_check.append(child)
                        find_play_all_buttons(child, depth + 1, max_d)
                except:
                    pass
            find_play_all_buttons(MAIN_WINDOW, 0, 20)
            if play_all_buttons_check:
                play_all_button_check = play_all_buttons_check[0]
                if play_all_button_check.Exists(0.1, 0.1):
                    break
    
    # 搜索 Name="play 播放全部" 的按钮
    try:
        # 首先精确匹配 "play 播放全部"
        play_all_buttons = []
        found_depths = []
        def find_play_all(ctrl, depth=0, max_d=20):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ButtonControl":
                        name = (child.Name or "").strip()
                        if name == "play 播放全部" or name.lower() == "play 播放全部" or "播放全部" in name:
                            play_all_buttons.append(child)
                            found_depths.append(depth + 1)
                            print(f"找到播放全部按钮 (深度: {depth + 1}, 名称: {name})")
                    find_play_all(child, depth + 1, max_d)
            except:
                pass
        find_play_all(MAIN_WINDOW, 0, 20)
        if play_all_buttons:
            print(f"共找到 {len(play_all_buttons)} 个播放全部按钮，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
        
        if play_all_buttons:
            play_all_button = play_all_buttons[0]
            if play_all_button.Exists(3, INTERVAL):
                try:
                    play_all_button.Click()
                    # 等待播放开始：检测播放控制按钮是否出现（播放开始后会有播放/暂停按钮）
                    play_control = MAIN_WINDOW.ButtonControl(Name='播放', searchDepth=MAX_SEARCH_DEPTH)
                    if not play_control.Exists(3, 0.1):
                        # 等待播放控制出现
                        for _ in range(20):
                            play_control = MAIN_WINDOW.ButtonControl(Name='播放', searchDepth=MAX_SEARCH_DEPTH)
                            if play_control.Exists(0.1, 0.1):
                                depth = _find_control_depth(MAIN_WINDOW, play_control, MAX_SEARCH_DEPTH)
                                print(f"找到播放控制按钮，深度: {depth}")
                                break
                    else:
                        depth = _find_control_depth(MAIN_WINDOW, play_control, MAX_SEARCH_DEPTH)
                        print(f"找到播放控制按钮，深度: {depth}")
                    playlist_info = f"歌单 '{playlist_name}'" if playlist_name else "当前歌单"
                    return f"成功: 已开始播放{playlist_info}的全部歌曲"
                except Exception as e:
                    return f"失败: 点击播放全部按钮时出错 - {str(e)}"
            else:
                return "失败: 找到播放全部按钮但无法访问"
        else:
            return "失败: 找不到播放全部按钮（Name='play 播放全部'）"
    except Exception as e:
        return f"失败: 搜索播放全部按钮时出错 - {str(e)}"


@require_uia
def search_and_open_playlist(playlist_name):
    """
    在全局搜索栏搜索歌单名称并打开歌单
    
    搜索后, 定位Name="歌单"的"选项卡项目"并Click, 然后在其父节点的父节点的下一个兄弟节点中搜索"行组",
    在该节点内定位第一个组并Click, 便能进入歌单
    
    Args:
        playlist_name (str): 歌单名称
    
    Returns:
        str: 操作结果描述
    """
    _ensure_netease_window_active()
    dropdown()
    
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        return "失败: 找不到网易云音乐主窗口"
    
    try:
        # 1. 在全局搜索栏搜索歌单名称
        if not ANCHOR or not ANCHOR.Exists(3, 1):
            return "失败: 找不到听歌识曲控件（搜索栏）"
        
        top_container = ANCHOR.GetParentControl()
        if not top_container:
            return "失败: 无法获取搜索栏的父容器"
        
        search_box = top_container.EditControl()
        if not search_box.Exists(3, INTERVAL):
            return "失败: 找不到搜索框"
        
        try:
            search_box.Click()
            # 使用 ValuePattern 设置文本，如果没有则使用 SendKeys
            try:
                # 尝试使用 ValuePattern
                value_pattern = search_box.GetValuePattern()
                if value_pattern:
                    value_pattern.SetValue(playlist_name)
                    print(f"使用 ValuePattern 设置文本: {playlist_name}")
                else:
                    raise AttributeError("ValuePattern not available")
            except (AttributeError, Exception) as e:
                # 如果 ValuePattern 失败，使用 SendKeys（先清空再输入）
                print(f"ValuePattern 不可用，使用 SendKeys: {e}")
                search_box.SendKeys('^a')  # Ctrl+A 全选
                time.sleep(0.1)  # 短暂等待
                search_box.SendKeys(playlist_name)
            search_box.SendKeys('{ENTER}')
            # 等待搜索结果加载：检测"歌单"选项卡是否出现
            playlist_tab = None
            for _ in range(20):  # 最多等待2秒
                # 直接查找 TabItemControl
                playlist_tab_obj = MAIN_WINDOW.TabItemControl(Name='歌单', searchDepth=MAX_SEARCH_DEPTH)
                if playlist_tab_obj.Exists(1, 0):
                    depth = _find_control_depth(MAIN_WINDOW, playlist_tab_obj, MAX_SEARCH_DEPTH)
                    print(f"找到'歌单'选项卡，深度: {depth}")
                    playlist_tab_items = [playlist_tab_obj]
                else:
                    playlist_tab_items = []
                if playlist_tab_items:
                    playlist_tab = playlist_tab_items[0]
                    if playlist_tab.Exists(0.1, 0.1):
                        break
        except Exception as e:
            return f"失败: 搜索歌单名称时出错 - {str(e)}"
        
        # 2. 定位Name="歌单"的"选项卡项目"并Click
        if not playlist_tab:
            # 直接查找 TabItemControl
            playlist_tab_obj = MAIN_WINDOW.TabItemControl(Name='歌单', searchDepth=MAX_SEARCH_DEPTH)
            if playlist_tab_obj.Exists(1, 0):
                depth = _find_control_depth(MAIN_WINDOW, playlist_tab_obj, MAX_SEARCH_DEPTH)
                print(f"找到'歌单'选项卡，深度: {depth}")
                playlist_tab_items = [playlist_tab_obj]
            else:
                playlist_tab_items = []
            if not playlist_tab_items:
                return "失败: 找不到'歌单'选项卡"
            playlist_tab = playlist_tab_items[0]
        
        if not playlist_tab.Exists(3, INTERVAL):
            return "失败: '歌单'选项卡不可访问"
        
        try:
            playlist_tab.Click()
            # 等待选项卡切换：检测行组是否出现
            # 这里会在后续步骤中检测
        except Exception as e:
            return f"失败: 点击'歌单'选项卡时出错 - {str(e)}"
        
        # 3. 在其父节点的父节点的下一个兄弟节点中搜索"行组"
        try:
            # 获取父节点的父节点
            parent = playlist_tab.GetParentControl()
            if not parent:
                return "失败: 无法获取'歌单'选项卡的父节点"
            
            grandparent = parent.GetParentControl()
            if not grandparent:
                return "失败: 无法获取'歌单'选项卡的父节点的父节点"
            
            # 获取下一个兄弟节点
            # 注意：uiautomation可能没有直接的GetNextSibling方法，需要遍历父节点的子节点
            siblings = grandparent.GetChildren()
            if not siblings:
                return "失败: 无法获取兄弟节点"
            
            # 找到当前父节点在兄弟节点中的位置
            current_index = -1
            for i, sibling in enumerate(siblings):
                if sibling == parent:
                    current_index = i
                    break
            
            if current_index == -1 or current_index >= len(siblings) - 1:
                return "失败: 无法找到下一个兄弟节点"
            
            next_sibling = siblings[current_index + 1]
            
            # 4. 在下一个兄弟节点中搜索"行组"（RowControl或GroupControl）
            row_groups = []
            found_depths = []
            def collect_row_groups(ctrl, depth=0, max_d=10):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName in ["RowControl", "GroupControl"]:
                            row_groups.append(child)
                            found_depths.append(depth + 1)
                            if len(row_groups) <= 5:
                                print(f"找到行组 (深度: {depth + 1}, 类型: {child.ControlTypeName}, 名称: {child.Name or 'N/A'})")
                        collect_row_groups(child, depth + 1, max_d)
                except:
                    pass
            collect_row_groups(next_sibling, 0, 10)
            if row_groups:
                print(f"共找到 {len(row_groups)} 个行组，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
            
            if not row_groups:
                return "失败: 在下一个兄弟节点中找不到行组"
            
            # 5. 在该节点内定位第一个组并Click
            first_group = row_groups[0]
            if not first_group.Exists(3, INTERVAL):
                return "失败: 第一个行组不可访问"
            
            try:
                first_group.Click()
                # 等待歌单打开：检测搜索框是否出现
                search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                if not search_box.Exists(3, 0.1):
                    for _ in range(20):
                        search_box = MAIN_WINDOW.EditControl(Name='搜索', searchDepth=MAX_SEARCH_DEPTH)
                        if search_box.Exists(0.1, 0.1):
                            depth = _find_control_depth(MAIN_WINDOW, search_box, MAX_SEARCH_DEPTH)
                            print(f"找到搜索框，深度: {depth}")
                            break
                else:
                    depth = _find_control_depth(MAIN_WINDOW, search_box, MAX_SEARCH_DEPTH)
                    print(f"找到搜索框，深度: {depth}")
                return f"成功: 已通过搜索打开歌单 '{playlist_name}'"
            except Exception as e:
                return f"失败: 点击第一个行组时出错 - {str(e)}"
                
        except Exception as e:
            return f"失败: 定位行组时出错 - {str(e)}"
            
    except Exception as e:
        return f"失败: 搜索并打开歌单时出错 - {str(e)}"


@require_uia
def collect_playlist(playlist_name=None):
    """
    收藏歌单
    
    切换歌单后搜索包含"collect"的按钮, 具体形式为"collect 数字"如"collect 6885"并Click
    
    Args:
        playlist_name (str, optional): 歌单名称。如果提供，会先切换到该歌单
    
    Returns:
        str: 操作结果描述
    """
    _ensure_netease_window_active()
    dropdown()
    
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        return "失败: 找不到网易云音乐主窗口"
    
    # 如果提供了歌单名称，先切换到该歌单
    if playlist_name:
        switch_result = switch_to_playlist(playlist_name)
        if switch_result.startswith("失败:") or switch_result.startswith("错误:"):
            return f"失败: 无法切换到歌单 '{playlist_name}' - {switch_result}"
        # 等待歌单切换完成：检测收藏按钮是否出现
        collect_button_check = None
        for _ in range(20):  # 最多等待2秒
            collect_buttons_check = []
            found_depths = []
            def find_collect_buttons(ctrl, depth=0, max_d=20):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName == "ButtonControl" and "collect" in (child.Name or "").lower():
                            collect_buttons_check.append(child)
                            found_depths.append(depth + 1)
                        find_collect_buttons(child, depth + 1, max_d)
                except:
                    pass
            find_collect_buttons(MAIN_WINDOW, 0, 20)
            if collect_buttons_check:
                print(f"找到 {len(collect_buttons_check)} 个收藏按钮（检查用），深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
            if collect_buttons_check:
                collect_button_check = collect_buttons_check[0]
                if collect_button_check.Exists(0.1, 0.1):
                    break
    
    # 搜索包含"collect"的按钮（形式为"collect 数字"）
    try:
        collect_buttons = []
        found_depths = []
        def find_collect_buttons(ctrl, depth=0, max_d=20):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ButtonControl" and "collect" in (child.Name or "").lower():
                        collect_buttons.append(child)
                        found_depths.append(depth + 1)
                        print(f"找到收藏按钮 (深度: {depth + 1}, 名称: {child.Name or 'N/A'})")
                    find_collect_buttons(child, depth + 1, max_d)
            except:
                pass
        find_collect_buttons(MAIN_WINDOW, 0, 20)
        if collect_buttons:
            print(f"共找到 {len(collect_buttons)} 个收藏按钮，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
        
        if not collect_buttons:
            return "失败: 找不到收藏按钮（包含'collect'的按钮）"
        
        # 找到第一个匹配的按钮
        collect_button = None
        for btn in collect_buttons:
            btn_name = (btn.Name or "").lower()
            # 检查是否符合"collect 数字"的格式
            if btn_name.startswith("collect") and any(char.isdigit() for char in btn_name):
                collect_button = btn
                break
        
        if not collect_button:
            # 如果找不到精确匹配，使用第一个包含collect的按钮
            collect_button = collect_buttons[0]
        
        if not collect_button.Exists(3, INTERVAL):
            return "失败: 找到收藏按钮但无法访问"
        
        try:
            collect_button.Click()
            # 等待操作完成：检测按钮状态是否变化（收藏后按钮可能会变化）
            # 或者检测是否有成功提示出现，这里简单等待按钮可访问即可
            if not collect_button.Exists(1, 0.1):
                # 按钮可能已变化，等待一下
                for _ in range(10):
                    if collect_button.Exists(0.1, 0.1):
                        break
            
            # 清除歌单缓存，因为收藏操作会改变歌单列表
            global PLAY_LIST
            PLAY_LIST = []
            
            playlist_info = f"歌单 '{playlist_name}'" if playlist_name else "当前歌单"
            return f"成功: 已收藏{playlist_info}"
        except Exception as e:
            return f"失败: 点击收藏按钮时出错 - {str(e)}"
            
    except Exception as e:
        return f"失败: 搜索收藏按钮时出错 - {str(e)}"


@require_uia
def uncollect_playlist(playlist_name=None):
    """
    取消收藏歌单
    
    先与上同理定位"collect"按钮并Click, 然后搜索Name="确定取消收藏该歌单？"的对话框,
    在该对话框内部搜索Name="confirm"的按钮并Click
    
    Args:
        playlist_name (str, optional): 歌单名称。如果提供，会先切换到该歌单
    
    Returns:
        str: 操作结果描述
    """
    _ensure_netease_window_active()
    dropdown()
    
    if not MAIN_WINDOW or not MAIN_WINDOW.Exists(3, INTERVAL):
        return "失败: 找不到网易云音乐主窗口"
    
    # 如果提供了歌单名称，先切换到该歌单
    if playlist_name:
        switch_result = switch_to_playlist(playlist_name)
        if switch_result.startswith("失败:") or switch_result.startswith("错误:"):
            return f"失败: 无法切换到歌单 '{playlist_name}' - {switch_result}"
        # 等待歌单切换完成：检测收藏按钮是否出现
        collect_button_check = None
        for _ in range(20):  # 最多等待2秒
            collect_buttons_check = []
            found_depths = []
            def find_collect_buttons(ctrl, depth=0, max_d=20):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        if child.ControlTypeName == "ButtonControl" and "collect" in (child.Name or "").lower():
                            collect_buttons_check.append(child)
                            found_depths.append(depth + 1)
                        find_collect_buttons(child, depth + 1, max_d)
                except:
                    pass
            find_collect_buttons(MAIN_WINDOW, 0, 20)
            if collect_buttons_check:
                print(f"找到 {len(collect_buttons_check)} 个收藏按钮（检查用），深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
            if collect_buttons_check:
                collect_button_check = collect_buttons_check[0]
                if collect_button_check.Exists(0.1, 0.1):
                    break
    
    try:
        # 1. 定位"collect"按钮并Click（与收藏歌单相同）
        collect_buttons = []
        found_depths = []
        def find_collect_buttons(ctrl, depth=0, max_d=20):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ButtonControl" and "collect" in (child.Name or "").lower():
                        collect_buttons.append(child)
                        found_depths.append(depth + 1)
                        print(f"找到收藏按钮 (深度: {depth + 1}, 名称: {child.Name or 'N/A'})")
                    find_collect_buttons(child, depth + 1, max_d)
            except:
                pass
        find_collect_buttons(MAIN_WINDOW, 0, 20)
        if collect_buttons:
            print(f"共找到 {len(collect_buttons)} 个收藏按钮，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
        
        if not collect_buttons:
            return "失败: 找不到收藏按钮（包含'collect'的按钮）"
        
        # 找到第一个匹配的按钮
        collect_button = None
        for btn in collect_buttons:
            btn_name = (btn.Name or "").lower()
            if btn_name.startswith("collect") and any(char.isdigit() for char in btn_name):
                collect_button = btn
                break
        
        if not collect_button:
            collect_button = collect_buttons[0]
        
        if not collect_button.Exists(3, INTERVAL):
            return "失败: 找到收藏按钮但无法访问"
        
        try:
            collect_button.Click()
            # 等待对话框出现：检测确认对话框是否存在
            dialog = None
            for _ in range(20):  # 最多等待2秒
                dialog_controls = []
                found_depths = []
                def find_dialog(ctrl, depth=0, max_d=20):
                    if depth > max_d:
                        return
                    try:
                        children = ctrl.GetChildren()
                        for child in children:
                            name = child.Name or ""
                            if "确定取消收藏该歌单" in name or ("取消收藏" in name and "确定" in name):
                                dialog_controls.append(child)
                                found_depths.append(depth + 1)
                                print(f"找到确认对话框 (深度: {depth + 1}, 名称: {name})")
                            find_dialog(child, depth + 1, max_d)
                    except:
                        pass
                find_dialog(MAIN_WINDOW, 0, 20)
                if dialog_controls:
                    print(f"共找到 {len(dialog_controls)} 个对话框，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
                if dialog_controls:
                    dialog = dialog_controls[0]
                    if dialog.Exists(0.1, 0.1):
                        break
        except Exception as e:
            return f"失败: 点击收藏按钮时出错 - {str(e)}"
        
        # 2. 搜索Name="确定取消收藏该歌单？"的对话框
        if not dialog:
            dialog_controls = []
            def find_dialog(ctrl, depth=0, max_d=20):
                if depth > max_d:
                    return
                try:
                    children = ctrl.GetChildren()
                    for child in children:
                        name = child.Name or ""
                        if "确定取消收藏该歌单" in name or ("取消收藏" in name and "确定" in name):
                            dialog_controls.append(child)
                        find_dialog(child, depth + 1, max_d)
                except:
                    pass
            find_dialog(MAIN_WINDOW, 0, 20)
            if not dialog_controls:
                return "失败: 找不到确认对话框（'确定取消收藏该歌单？'）"
            dialog = dialog_controls[0]
        
        if not dialog.Exists(3, INTERVAL):
            return "失败: 找到确认对话框但无法访问"
        
        # 3. 在该对话框内部搜索Name="confirm"的按钮并Click
        confirm_buttons = []
        found_depths = []
        def find_confirm_buttons(ctrl, depth=0, max_d=10):
            if depth > max_d:
                return
            try:
                children = ctrl.GetChildren()
                for child in children:
                    if child.ControlTypeName == "ButtonControl":
                        name = (child.Name or "").lower()
                        if name == "confirm" or "confirm" in name:
                            confirm_buttons.append(child)
                            found_depths.append(depth + 1)
                            print(f"找到确认按钮 (深度: {depth + 1}, 名称: {child.Name or 'N/A'})")
                    find_confirm_buttons(child, depth + 1, max_d)
            except:
                pass
        find_confirm_buttons(dialog, 0, 10)
        if confirm_buttons:
            print(f"共找到 {len(confirm_buttons)} 个确认按钮，深度范围: {min(found_depths) if found_depths else 'N/A'} - {max(found_depths) if found_depths else 'N/A'}")
        
        if not confirm_buttons:
            return "失败: 在对话框中找不到'confirm'按钮"
        
        confirm_button = confirm_buttons[0]
        if not confirm_button.Exists(3, INTERVAL):
            return "失败: 找到'confirm'按钮但无法访问"
        
        try:
            confirm_button.Click()
            # 等待操作完成：检测对话框是否消失（操作成功后对话框会关闭）
            for _ in range(20):  # 最多等待2秒
                if not dialog.Exists(0.1, 0.1):
                    break
            
            # 清除歌单缓存，因为取消收藏操作会改变歌单列表
            global PLAY_LIST
            PLAY_LIST = []
            
            playlist_info = f"歌单 '{playlist_name}'" if playlist_name else "当前歌单"
            return f"成功: 已取消收藏{playlist_info}"
        except Exception as e:
            return f"失败: 点击'confirm'按钮时出错 - {str(e)}"
            
    except Exception as e:
        return f"失败: 取消收藏歌单时出错 - {str(e)}"


@require_uia
def control_netease_music(action):
    """
    控制网易云音乐播放（播放/暂停/上一首/下一首/音量等）
    使用全局快捷键完成操作
    
    Args:
        action (str): 操作类型
            - "play": 播放
            - "pause": 暂停
            - "play_pause": 播放/暂停切换
            - "next": 下一首
            - "previous": 上一首
            - "volume_up": 音量增加
            - "volume_down": 音量减少
            - "like": 喜欢(收藏)
            - "unlike": 不喜欢(取消收藏)
    Returns:
        str: 操作结果描述
    """
    if not PY_AUTOGUI_AVAILABLE:
        return "失败: 未安装 pyautogui 库，无法使用全局快捷键"
    
    global MAIN_WINDOW
    
    # 确保窗口存在并激活（快捷键需要在窗口激活时才能生效）
    if not MAIN_WINDOW:
        _ensure_netease_window_active()

    if not MAIN_WINDOW.Exists(3, INTERVAL):
        _ensure_netease_window_active()
        if not MAIN_WINDOW.Exists(3, INTERVAL):
            return "错误: 找不到网易云音乐主窗口"
    
        # 激活窗口以确保快捷键能正确发送
        try:
            MAIN_WINDOW.SetActive()
            # 等待窗口激活：检测ANCHOR是否可见
            if not ANCHOR or not ANCHOR.Exists(1, 0.1):
                ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH)
                if not ANCHOR.Exists(1, 0.1):
                    for _ in range(10):
                        ANCHOR = MAIN_WINDOW.GroupControl(Name='听歌识曲', searchDepth=MAX_SEARCH_DEPTH)
                        if ANCHOR.Exists(0.1, 0.1):
                            break
        except:
            pass
    
    # 使用全局快捷键执行操作
    try:
        if action == "play" or action == "play_pause":
            # 播放/暂停：空格键
            pyautogui.hotkey('ctrl', 'alt', 'p')
        elif action == "pause":
            # 暂停：空格键
            pyautogui.hotkey('ctrl', 'alt', 'p')
        elif action == "next":
            # 下一首：Ctrl+Right
            pyautogui.hotkey('ctrl', 'alt', 'right')
        elif action == "previous":
            # 上一首：Ctrl+Left
            pyautogui.hotkey('ctrl', 'alt', 'left')
        elif action == "volume_up":
            # 音量增加：Ctrl+Up
            pyautogui.hotkey('ctrl', 'alt', 'up')
        elif action == "volume_down":
            # 音量减少：Ctrl+Down
            pyautogui.hotkey('ctrl', 'alt', 'down')
        elif action == "like":
            # 喜欢：Ctrl+Shift+L
            pyautogui.hotkey('ctrl', 'alt', 'l')
        elif action == "unlike":
            # 不喜欢：Ctrl+Shift+D
            pyautogui.hotkey('ctrl', 'alt', 'l')
        else:
            return f"失败: 不支持的操作类型 '{action}'"
        
        # 返回成功状态
        action_map = {
            "play": "播放",
            "pause": "暂停",
            "play_pause": "播放/暂停切换",
            "next": "下一首",
            "previous": "上一首",
            "volume_up": "音量增加",
            "volume_down": "音量减少",
            "like": "喜欢(收藏)",
            "unlike": "不喜欢(取消收藏)",
        }
        action_name = action_map.get(action, action)
        return f"成功: 已{action_name}"
    except Exception as e:
        return f"错误: 执行操作时出错: {str(e)}"


if __name__ == "__main__":
    # 测试套件
    # 1. 基础功能测试
    # print("\n[1] 测试：打开网易云音乐应用")
    # print("-" * 60)
    # result = open_netease_music()
    # print(f"结果: {result}")
    # time.sleep(3)
    
    # print("\n[2] 测试：确保窗口激活")
    # print("-" * 60)
    # _ensure_netease_window_active()
    # print("结果: 窗口已激活")
    # time.sleep(1)
    
    # print("\n[3] 测试：调用dropdown函数")
    # print("-" * 60)
    # d = dropdown()
    # print(d)
    # print("结果: dropdown函数已执行")
    # time.sleep(1)
    
    # # # 2. 搜索和播放测试
    # # print("\n[4] 测试：搜索音乐")
    # # print("-" * 60)
    # # result = search_music_in_netease("grapefruit moon")
    # # print(f"结果: {result}")
    # # time.sleep(2)
    
    print("\n[5] 测试：播放音乐")
    print("-" * 60)
    result = play_music_in_netease("星と僕ら")
    print(f"结果: {result}")
    time.sleep(2)
    
    # # 3. 播放控制测试
    # print("\n[6] 测试：播放控制 - 播放/暂停")
    # print("-" * 60)
    # result = control_netease_music("play_pause")
    # print(f"结果: {result}")
    # time.sleep(1)
    
    # print("\n[7] 测试：播放控制 - 下一首")
    # print("-" * 60)
    # result = control_netease_music("next")
    # print(f"结果: {result}")
    # time.sleep(1)
    
    # print("\n[8] 测试：播放控制 - 上一首")
    # print("-" * 60)
    # result = control_netease_music("previous")
    # print(f"结果: {result}")
    # time.sleep(1)
    
    # print("\n[9] 测试：播放控制 - 音量增加")
    # print("-" * 60)
    # result = control_netease_music("volume_up")
    # print(f"结果: {result}")
    # time.sleep(1)
    
    # print("\n[10] 测试：播放控制 - 音量减少")
    # print("-" * 60)
    # result = control_netease_music("volume_down")
    # print(f"结果: {result}")
    # time.sleep(1)
    
    # # print("\n[11] 测试：播放控制 - 喜欢")
    # # print("-" * 60)
    # # result = control_netease_music("like")
    # # print(f"结果: {result}")
    # # time.sleep(1)
    
    # # 4. 歌单相关功能测试
    # print("\n[12] 测试：获取所有歌单名称")
    # print("-" * 60)
    # result = get_playlist_name_in_netease(force_refresh=True)
    # if result:
    #     print(f"结果: 成功获取 {len(result)} 个歌单")
    #     # print(f"前5个歌单: {result[:5] if len(result) >= 5 else result}")
    #     print(f"歌单: {result}")
    # else:
    #     print("结果: 获取歌单失败或歌单列表为空")
    # time.sleep(2)
    
    # # 5. 切换到歌单测试（如果有歌单的话）
    # if result and len(result) > 0:
    #     test_playlist = result[0]  # 使用第一个歌单进行测试
    #     print(f"\n[13] 测试：切换到歌单 '{test_playlist}'")
    #     print("-" * 60)
    #     switch_result = switch_to_playlist(test_playlist)
    #     print(f"结果: {switch_result}")
    #     time.sleep(2)
        
    #     print(f"\n[14] 测试：播放歌单全部歌曲")
    #     print("-" * 60)
    #     play_all_result = play_all_songs_in_playlist(test_playlist)
    #     print(f"结果: {play_all_result}")
    #     time.sleep(2)
        
    #     print(f"\n[15] 测试：在歌单中播放歌曲")
    #     print("-" * 60)
    #     play_result = play_song_from_playlist(playlist_name=test_playlist, song_name="星星与我们")
    #     print(f"结果: {play_result}")
    #     time.sleep(2)
        
    #     # 6. 搜索并打开歌单测试
    #     print(f"\n[16] 测试：搜索并打开歌单 '{test_playlist}'")
    #     print("-" * 60)
    #     search_result = search_and_open_playlist(test_playlist)
    #     print(f"结果: {search_result}")
    #     time.sleep(2)
        
    #     # # 7. 收藏功能测试（谨慎测试，避免重复收藏）
    #     # print(f"\n[17] 测试：收藏歌单 '{test_playlist}'")
    #     # print("-" * 60)
    #     # print("注意: 如果歌单已收藏，此操作可能会失败")
    #     # collect_result = collect_playlist(test_playlist)
    #     # print(f"结果: {collect_result}")
    #     # time.sleep(2)
        
    #     # # 8. 取消收藏测试（谨慎测试）
    #     # print(f"\n[18] 测试：取消收藏歌单 '{test_playlist}'")
    #     # print("-" * 60)
    #     # print("注意: 如果歌单未收藏，此操作可能会失败")
    #     # uncollect_result = uncollect_playlist(test_playlist)
    #     # print(f"结果: {uncollect_result}")
    #     # time.sleep(2)
    # else:
    #     print("\n[13-18] 跳过：未找到歌单，跳过歌单相关测试")
    
    # # 9. 测试无参数调用
    # print("\n[19] 测试：播放当前歌单中的歌曲（无参数）")
    # print("-" * 60)
    # play_result = play_song_from_playlist(song_name="稻香")
    # print(f"结果: {play_result}")
    # time.sleep(2)
    
    # print("\n[20] 测试：播放当前歌单全部歌曲（无参数）")
    # print("-" * 60)
    # play_all_result = play_all_songs_in_playlist()
    # print(f"结果: {play_all_result}")
    # time.sleep(2)