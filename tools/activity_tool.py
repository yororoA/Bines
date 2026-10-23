"""Yield, continue or pause a persistent autonomous activity."""
import re

from tools.context import CURRENT_CONTEXT


def activity_continue(next_intent: str, note: str = "", delay_seconds: float = 2) -> dict:
    """保存活动进度并继续；delay_seconds 是观察/播放间隔，每批执行后让出处理消息的机会。"""
    context = CURRENT_CONTEXT.get()
    if not context.autonomous:
        raise PermissionError("activity_continue 仅供可信自主活动使用")
    if not 2 <= len(next_intent.strip()) <= 500 or not 1 <= float(delay_seconds) <= 3600:
        raise ValueError("next_intent 需 2–500 字，delay_seconds 需 1–3600 秒")
    context.activity.update(mode="continue", next_intent=next_intent.strip(),
                            note=note[:2000], delay_seconds=float(delay_seconds))
    return {"status": "ok", "activity_status": "continuing"}


def activity_finish(note: str = "") -> dict:
    """目标完成或兴趣消退时结束当前活动；不会虚构用户回复。"""
    context = CURRENT_CONTEXT.get()
    if not context.autonomous:
        raise PermissionError("仅供自主活动使用")
    context.activity.update(mode="finish", note=note[:2000])
    return {"status": "ok", "activity_status": "finished"}


def activity_pause(reason: str, delay_seconds: float = 60) -> dict:
    """活动暂时受阻时保存进度并暂停；登录/验证码用 delay_seconds=0，等待主人恢复。"""
    context = CURRENT_CONTEXT.get()
    if not context.autonomous:
        raise PermissionError("仅供自主活动使用")
    if not 0 <= float(delay_seconds) <= 3600:
        raise ValueError("delay_seconds 需 0–3600 秒")
    context.activity.update(mode="pause", note=reason[:2000], delay_seconds=float(delay_seconds))
    return {"status": "ok", "activity_status": "paused"}


def autonomy_control(mode: str, reason: str = "") -> dict:
    """主人控制自主活动：pause 暂停、resume 恢复、stop 结束当前活动；自主轮次不能调用。"""
    context = CURRENT_CONTEXT.get()
    if not context.identity.privileged or context.autonomous or not context.user_text:
        raise PermissionError("自主活动控制仅允许主人本轮明确要求")
    words = {"pause": ("暂停", "停一下", "别操作"), "resume": ("恢复", "继续"), "stop": ("停止", "结束")}
    if mode not in words or not any(word in context.user_text for word in words[mode]):
        raise PermissionError("当前主人消息未要求对应控制")
    verbs = "|".join(re.escape(word) for word in words[mode])
    if re.search(rf"(?:不要|不用|别|不必|不)\s*(?:{verbs})", context.user_text):
        raise PermissionError("当前主人消息否定了对应控制")
    from common.activity import set_owner_control
    return {"status": "ok", "control": set_owner_control(mode, reason)}
