"""
指针与键盘工具集：基于坐标的鼠标操作（单击、双击、拖拽）与键盘输入（打字、组合键）。
坐标均为屏幕像素 (x, y)。
"""
import time
from typing import List


def _ensure_pyautogui():
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        return pyautogui
    except ImportError:
        raise RuntimeError("缺少依赖 pyautogui，请安装: pip install pyautogui")


def _clamp_coords(x: int, y: int):
    """将坐标裁剪到当前屏幕范围内。"""
    try:
        import pyautogui
        w, h = pyautogui.size()
        return max(0, min(x, w - 1)), max(0, min(y, h - 1))
    except Exception:
        return x, y


# ---------------------------------------------------------------------------
# 1. 左键单击、左键双击、右键单击（参数：坐标位置，连续次数）
# ---------------------------------------------------------------------------

def left_click(x: int, y: int, times: int = 1) -> str:
    """
    在指定坐标左键单击，可连续多次。
    Args:
        x: 横坐标（像素）
        y: 纵坐标（像素）
        times: 连续点击次数，默认 1
    """
    p = _ensure_pyautogui()
    x, y = _clamp_coords(x, y)
    for i in range(max(1, int(times))):
        p.click(x, y, button="left")
        if i < times - 1:
            time.sleep(0.1)
    return f"已在 ({x},{y}) 左键单击 {times} 次"


def left_double_click(x: int, y: int, times: int = 1) -> str:
    """
    在指定坐标左键双击，可连续多次（每次为一次“双击”）。
    Args:
        x: 横坐标（像素）
        y: 纵坐标（像素）
        times: 连续双击次数，默认 1
    """
    p = _ensure_pyautogui()
    x, y = _clamp_coords(x, y)
    for i in range(max(1, int(times))):
        p.doubleClick(x, y, button="left")
        if i < times - 1:
            time.sleep(0.15)
    return f"已在 ({x},{y}) 左键双击 {times} 次"


def right_click(x: int, y: int, times: int = 1) -> str:
    """
    在指定坐标右键单击，可连续多次。
    Args:
        x: 横坐标（像素）
        y: 纵坐标（像素）
        times: 连续点击次数，默认 1
    """
    p = _ensure_pyautogui()
    x, y = _clamp_coords(x, y)
    for i in range(max(1, int(times))):
        p.click(x, y, button="right")
        if i < times - 1:
            time.sleep(0.1)
    return f"已在 ({x},{y}) 右键单击 {times} 次"


# ---------------------------------------------------------------------------
# 2. 左键按下拖拽、右键按下拖拽（参数：起点坐标，终点坐标）
# ---------------------------------------------------------------------------

def left_drag(start_x: int, start_y: int, end_x: int, end_y: int) -> str:
    """
    左键按下从起点拖拽到终点。
    Args:
        start_x, start_y: 起点坐标（像素）
        end_x, end_y: 终点坐标（像素）
    """
    p = _ensure_pyautogui()
    start_x, start_y = _clamp_coords(start_x, start_y)
    end_x, end_y = _clamp_coords(end_x, end_y)
    p.moveTo(start_x, start_y, duration=0.1)
    time.sleep(0.05)
    p.drag(end_x - start_x, end_y - start_y, duration=0.2, button="left")
    return f"已左键拖拽 ({start_x},{start_y}) -> ({end_x},{end_y})"


def right_drag(start_x: int, start_y: int, end_x: int, end_y: int) -> str:
    """
    右键按下从起点拖拽到终点。
    Args:
        start_x, start_y: 起点坐标（像素）
        end_x, end_y: 终点坐标（像素）
    """
    p = _ensure_pyautogui()
    start_x, start_y = _clamp_coords(start_x, start_y)
    end_x, end_y = _clamp_coords(end_x, end_y)
    p.moveTo(start_x, start_y, duration=0.1)
    time.sleep(0.05)
    p.drag(end_x - start_x, end_y - start_y, duration=0.2, button="right")
    return f"已右键拖拽 ({start_x},{start_y}) -> ({end_x},{end_y})"


# ---------------------------------------------------------------------------
# 3. 打字（参数：要打出的字符串）
# ---------------------------------------------------------------------------

def type_text(text: str) -> str:
    """
    在当前焦点位置模拟键盘输入字符串。
    Args:
        text: 要输入的字符串（支持英文与常见符号；中文依赖输入法状态）
    """
    p = _ensure_pyautogui()
    if not text:
        return "未提供要输入的文字"
    p.write(text, interval=0.05)
    return f"已输入: {text[:50]}{'...' if len(text) > 50 else ''}"


# ---------------------------------------------------------------------------
# 4. 按键组合（参数：按键列表，一次性按下）
# ---------------------------------------------------------------------------

def hotkey(keys: List[str]) -> str:
    """
    一次性按下多个键（组合键），如 Ctrl+C、Alt+Tab。
    Args:
        keys: 按键列表，按顺序按下后同时释放，例如 ["ctrl", "c"] 表示 Ctrl+C
    """
    p = _ensure_pyautogui()
    if not keys or not isinstance(keys, (list, tuple)):
        return "请提供按键列表，例如 [\"ctrl\", \"c\"]"
    keys = [str(k).strip().lower() for k in keys if str(k).strip()]
    if not keys:
        return "按键列表为空"
    p.hotkey(*keys)
    return f"已按下组合键: {'+'.join(keys)}"
