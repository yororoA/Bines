import datetime
import random
import re
from typing import Optional, Tuple


_SYSTEM_MARKERS = [
    "[system event:",
    "[system event :",
    "system event:",
    "[screen monitor]",
    "[screen monitor:",
    "screen monitor:",
]

_REJECT_KEYWORDS = [
    "不看", "不要看", "别看了", "不用看", "不需要看",
    "ignore", "don't look", "stop watching",
    "不要分析", "不用分析", "别分析", "不需要分析",
]


def compute_enable_audio(source: Optional[str]) -> bool:
    """根据消息来源决定是否启用语音输出。"""
    source_str = str(source).strip() if source else ""
    enable_audio = source_str not in ["QQ", "qq"]
    if source_str in ["ASR", "MANUAL"]:
        enable_audio = True
    return enable_audio


def sanitize_user_input(user_input: str) -> Tuple[str, bool, bool]:
    """
    清洗输入中的伪系统标记。
    Returns:
        (cleaned_text, changed, reverted_to_original)
    """
    cleaned_user_input = user_input
    if not user_input:
        return cleaned_user_input, False, False

    for marker in _SYSTEM_MARKERS:
        cleaned_user_input = re.sub(re.escape(marker), "", cleaned_user_input, flags=re.IGNORECASE)
        cleaned_user_input = cleaned_user_input.strip()

    if not cleaned_user_input:
        return user_input, False, True

    return cleaned_user_input, cleaned_user_input != user_input, False


def build_time_gap_instruction(memory_system, user_input: str, current_date_obj: datetime.datetime) -> str:
    """构建“久别重逢”提示词（保持原逻辑）。"""
    try:
        history = memory_system.short_term.get_messages()
        last_date_obj = None

        for msg in reversed(history):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                match = re.search(r"\[(\d{4}/\d{1,2}/\d{1,2})\]:", content)
                if match:
                    try:
                        last_date_obj = datetime.datetime.strptime(match.group(1), "%Y/%m/%d")
                        break
                    except ValueError:
                        pass

        if not last_date_obj:
            return ""

        delta_days = (current_date_obj - last_date_obj).days
        wakeup_words = ["你好", "在吗", "醒醒", "启动", "喂", "hi", "hello"]
        is_wakeup = any(w in user_input.lower() for w in wakeup_words)

        if delta_days >= 1 and (is_wakeup or random.random() < 0.3):
            return (
                f"\nSystem Context: The last conversation was on {last_date_obj.strftime('%Y/%m/%d')}, "
                f"which is {delta_days} days ago. "
                "Since the user has been gone for a while, you should playfully complain "
                "about their long absence or the gap in time based on your persona."
            )
        return ""
    except Exception:
        return ""


def update_screen_monitor_rejection_state(user_input: str, rejected: bool, expire_time: float, now_ts: float):
    """
    根据当前输入与时间推进屏幕监控拒绝状态。
    Returns:
        (new_rejected, new_expire_time, set_rejected, recovered)
    """
    user_input_lower = (user_input or "").lower()

    set_rejected = any(keyword in user_input_lower for keyword in _REJECT_KEYWORDS)
    if set_rejected:
        return True, now_ts + 300, True, False

    if rejected and now_ts > expire_time:
        return False, expire_time, False, True

    return rejected, expire_time, False, False
