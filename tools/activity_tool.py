"""Yield, continue or pause a persistent autonomous activity."""
import re

from tools.context import CURRENT_CONTEXT


def _authorize_activity():
    context = CURRENT_CONTEXT.get()
    owner_turn = (context.identity.privileged and context.event_id
                  and context.turn_id and context.user_text)
    if not context.autonomous and not owner_turn:
        raise PermissionError("活动进度仅供可信自主轮次或主人当前消息使用")
    return context


def activity_continue(next_intent: str, note: str = "", delay_seconds: float = 2,
                      reason: str = "", topic: str = "") -> dict:
    """自己选择继续或换方向，保存兴趣理由与下一步；观察间隔也由你选择。"""
    context = _authorize_activity()
    if not 2 <= len(next_intent.strip()) <= 500 or not 1 <= float(delay_seconds) <= 3600:
        raise ValueError("next_intent 需 2–500 字，delay_seconds 需 1–3600 秒")
    chosen_reason = reason[:500] or note[:500] or context.activity.get("reason", "")
    chosen_topic = topic[:80] or context.activity.get("topic", "")
    context.activity.update(mode="continue", next_intent=next_intent.strip(),
                            note=note[:2000], delay_seconds=float(delay_seconds),
                            reason=chosen_reason, topic=chosen_topic,
                            decision_source="model",
                            decision={"mode": "continue", "next_intent": next_intent.strip(),
                                      "reason": chosen_reason, "topic": chosen_topic,
                                      "delay_seconds": float(delay_seconds)})
    return {"status": "ok", "activity_status": "continuing"}


def activity_finish(note: str = "") -> dict:
    """目标完成或兴趣消退时结束当前活动；不会虚构用户回复。"""
    context = _authorize_activity()
    context.activity.update(mode="finish", note=note[:2000], decision_source="model",
                            decision={"mode": "finish", "reason": note[:500]})
    return {"status": "ok", "activity_status": "finished"}


def activity_pause(reason: str, delay_seconds: float = 60) -> dict:
    """活动暂时受阻时保存进度并暂停；登录/验证码用 delay_seconds=0，等待主人恢复。"""
    context = _authorize_activity()
    if not 0 <= float(delay_seconds) <= 3600:
        raise ValueError("delay_seconds 需 0–3600 秒")
    context.activity.update(mode="pause", note=reason[:2000], delay_seconds=float(delay_seconds),
                            decision_source="model", decision={"mode": "pause", "reason": reason[:500],
                                                               "delay_seconds": float(delay_seconds)})
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
    control = set_owner_control(mode, reason)
    if mode in {"pause", "stop"}:
        # Commit the owner's choice to the current goal as well as the global
        # switch, so a durable activity is not left falsely active forever.
        context.activity.update(mode="finish" if mode == "stop" else "pause",
                                note=reason or context.user_text[:500], delay_seconds=0,
                                decision_source="owner")
    return {"status": "ok", "control": control}
