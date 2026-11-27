"""One timezone-aware clock for tools, conversation prompts and memory dates."""
from datetime import datetime
import time
from zoneinfo import ZoneInfo

from common.env import get_env

CLOCK_MARKER = "--- 当前真实时间 ---"
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def timezone():
    return ZoneInfo((get_env("BINES_MEMORY_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai").strip())


def now(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else float(timestamp), timezone())


def format_time(timestamp=None):
    current = now(timestamp)
    offset = current.strftime("%z")
    offset = offset[:3] + ":" + offset[3:]
    return (current.strftime(f"%Y年%m月%d日 {_WEEKDAYS[current.weekday()]} %H:%M:%S")
            + f"（{current.tzinfo.key}，UTC{offset}）")


def clock_message(timestamp=None):
    return {
        "role": "system",
        "content": (
            f"{CLOCK_MARKER}\n{format_time(timestamp)}\n"
            "这是系统采样时的真实时钟；同轮有多次采样时，以最后一条为准。今天、昨天、明天及早晚均以这个时区和日期为准。"
            "历史消息、日记、网页日期、旧工具回执和状态中的 current_time 都只代表当时，不能作为现在。"
            "无需猜测当前时间；需要确认精确时间或经过较长操作后再报时，调用 get_time 刷新。"
            "这个时区不代表所有 QQ 对话者的所在地，未明确时不要推断对方的当地时间。"
        ),
    }


def with_current_time(messages, timestamp=None):
    """Keep one explanation and a bounded tail of trusted clock samples."""
    result = [dict(message) for message in messages]
    sample = clock_message(timestamp)
    clocks = [i for i, message in enumerate(result)
              if message.get("role") == "system"
              and str(message.get("content") or "").startswith(CLOCK_MARKER)]
    if not clocks:
        return [*result, sample]
    stamp = sample["content"].splitlines()[:2]
    previous = result[clocks[-1]]["content"].splitlines()[:2]
    # Preserve the original prompt-frame clock and its interpretation rules.
    # Later samples need only the date/time, never another copy of those rules.
    if previous != stamp:
        result.append({"role": "system", "content": "\n".join(stamp)})
        clocks.append(len(result) - 1)
    if len(clocks) > 4:
        expired = set(clocks[1:-3])
        result = [message for i, message in enumerate(result) if i not in expired]
    return result
