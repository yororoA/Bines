from typing import Dict, Optional


def should_run_idle_check(now: float, last_check_time: float, interval_sec: float) -> bool:
    return (now - last_check_time) >= interval_sec


def build_realtime_screen_synthetic_prompt(screen_content: str) -> str:
    return (
        "[系统：实时屏幕分析已更新] 当前屏幕发生显著变化。内容如下：\n"
        f"{screen_content}\n"
        "请判断是否需要对用户当前的屏幕操作做出反应。如果不需要，请直接输出空字符串或不输出任何内容；"
        "如果需要，请简短评论。不要输出无意义的动作描述。"
    )


def evaluate_idle_realtime_push(
    *,
    now: float,
    last_check_time: float,
    last_pushed_content: str,
    last_push_mtime: float,
    screen_content: str,
    game_mode_enabled: bool,
    is_processing: bool,
    is_player_busy: bool,
) -> Dict[str, Optional[object]]:
    """
    根据最新 realtime screen 内容计算状态更新与是否触发主动推送。
    返回字段：
      - last_check_time: float
      - last_pushed_content: str
      - last_push_mtime: float
      - should_trigger: bool
      - synthetic_prompt: Optional[str]
    """
    result: Dict[str, Optional[object]] = {
        "last_check_time": now,
        "last_pushed_content": last_pushed_content,
        "last_push_mtime": last_push_mtime,
        "should_trigger": False,
        "synthetic_prompt": None,
    }

    if not screen_content or screen_content == last_pushed_content:
        return result

    # 内容变化，先更新状态，避免重复检测
    result["last_pushed_content"] = screen_content
    result["last_push_mtime"] = now

    # 仅游戏模式下主动推送
    if not game_mode_enabled:
        return result

    # 忙碌时不推送
    if is_processing or is_player_busy:
        return result

    result["should_trigger"] = True
    result["synthetic_prompt"] = build_realtime_screen_synthetic_prompt(screen_content)
    return result
