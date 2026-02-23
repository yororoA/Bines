"""
游戏模式控制工具
用于启用/禁用游戏模式，控制屏幕监控频率
"""
from .dependencies import deps

def enable_game_mode(interval=0.1):
    """
    启用游戏模式
    
    Args:
        interval: 监控间隔（秒），默认0.1秒
    
    Returns:
        str: 操作结果消息
    """
    deps.set_game_mode(True, interval)
    return f"游戏模式已启用，监控间隔: {interval}秒"

def disable_game_mode():
    """
    禁用游戏模式
    
    Returns:
        str: 操作结果消息
    """
    deps.set_game_mode(False)
    return "游戏模式已禁用，恢复到普通模式"
