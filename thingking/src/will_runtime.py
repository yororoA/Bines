"""Execute one quantum of a persistent autonomous activity."""
import json
import os
import re
import subprocess
import ast
import time
import logging

from common.proactive import intention_prompt, ProactiveConfig
from common.memory_journal import outcome_status
log = logging.getLogger("bines.will")

READ_ACTIONS = {
    "get_time", "search_memory", "browser_search", "web_fetch",
    "napcat_discover", "napcat_catalog", "napcat_events", "napcat_operation", "napcat_custom_faces",
    "get_screen_info", "get_visual_info", "fast_screen_analysis", "template_match", "find_color_region",
    "get_qq_group_list", "get_qq_friend_list", "get_moments", "get_comments",
    "website_status", "website_open", "website_snapshot", "website_scroll", "website_plan",
    "website_media_status", "website_watch", "website_tabs",
}
DESKTOP_ACTIONS = {
    "open_application", "automate_action", "automate_sequence", "find_element_and_click",
    "find_element_and_type", "analyze_and_operate", "smart_click", "smart_type",
    "left_click", "left_double_click", "right_click", "left_drag", "right_drag", "type_text", "hotkey",
}
CONTACT_ACTIONS = {
    "napcat_send", "napcat_forward", "napcat_send_custom_face", "send_qq_group_msg", "send_qq_private_msg",
    "broadcast_to_all_friends", "broadcast_to_all_groups", "at_each_group_member",
    "add_moment", "comment_moment", "send_ark_chat", "like_moment", "like_comment",
    "website_approve", "website_execute",
}
PLANNING_ACTIONS = {"get_time", "search_memory", "regulate_life",
                    "activity_continue", "activity_pause", "activity_finish"}


class ActivityYield(Exception):
    """End a quantum, not the underlying activity."""


def outcome_succeeded(item):
    return outcome_status(item.get("result", "")) == "succeeded"


def desktop_idle_seconds():
    """Return None when OS input activity cannot be verified."""
    try:
        import sys
        if sys.platform == "darwin":
            result = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "1"],
                                    capture_output=True, text=True, timeout=2)
            match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
            return int(match[1]) / 1e9 if match else None
        if os.name == "nt":
            import ctypes
            class LastInput(ctypes.Structure):
                _fields_ = [("size", ctypes.c_uint), ("tick", ctypes.c_uint)]
            info = LastInput()
            info.size = ctypes.sizeof(info)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return ((ctypes.windll.kernel32.GetTickCount() - info.tick) & 0xffffffff) / 1000
        if sys.platform.startswith("linux"):
            result = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=2)
            return int(result.stdout.strip()) / 1000 if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


class WillSkillRunner:
    """Delegate to the installed runner, checking cancellation before every actual action."""
    def __init__(self, runner, candidate, check, *, desktop=False, ordinary=False):
        self.runner, self.candidate, self.check = runner, candidate, check
        self.desktop = desktop
        self.ordinary = ordinary
        self.outcomes = []
        self.last_desktop_action = 0
        self.called = 0
        self.started = time.monotonic()
        self.config = ProactiveConfig.from_env()

    def __getattr__(self, name):
        return getattr(self.runner, name)

    def function_tools(self, *args, **kwargs):
        tools = self.runner.function_tools(*args, **kwargs)
        if self.candidate.get("planning_only"):
            return [tool for tool in tools if tool.get("function", {}).get("name") in PLANNING_ACTIONS]
        return tools

    def execute(self, command, **kwargs):
        from tools.context import CURRENT_CONTEXT
        journal = CURRENT_CONTEXT.get().journal
        def refused(result):
            if journal:
                journal.capture(command.get("action"), command.get("args"), result,
                                category="tool", status="skipped")
            return result
        context = CURRENT_CONTEXT.get()
        if context.activity.get("mode") in {"continue", "finish", "pause", "review"}:
            raise ActivityYield()
        self.called += 1
        if (self.called > self.config.quantum_actions
                or (self.outcomes and time.monotonic() - self.started >= self.config.quantum_seconds)):
            context.activity.update(mode="review", note="执行权已让出，下一轮由你自主选择方向",
                                    delay_seconds=context.activity.get("delay_seconds", 2),
                                    decision_source="runtime")
            raise ActivityYield()
        reason = self.check()
        if reason:
            refused(f"自主行动已取消：{reason}")
            raise RuntimeError(f"自主行动已取消：{reason}")
        action = command.get("action")
        if self.candidate.get("planning_only") and action not in PLANNING_ACTIONS:
            return refused("自主行动未执行：休息期间本批只允许内部思考和生命周期调节")
        if not self.candidate.get("contact_allowed") and action in CONTACT_ACTIONS | {"mcp_call"}:
            return refused("自主行动未执行：当前只允许不打扰他人的查询或观察")
        if action in DESKTOP_ACTIONS and not self.ordinary:
            idle = desktop_idle_seconds() if self.desktop else None
            last_input = time.time() - idle if idle is not None else time.time()
            owned_input = self.last_desktop_action and last_input <= self.last_desktop_action + 0.5
            if idle is None or (idle < 60 and not owned_input):
                return refused("自主行动未执行：电脑正在使用或无法确认键鼠空闲")
        result = self.runner.execute(command, **kwargs)
        if action in DESKTOP_ACTIONS:
            self.last_desktop_action = time.time()
        self.outcomes.append({"action": action, "args": command.get("args", {}),
                              "result": str(result), "status": outcome_status(result)})
        from common.memory_journal import result_data
        data = result_data(result)
        if isinstance(data, dict) and (data.get("human_action_required")
                                      or data.get("status") == "uncertain"):
            context.activity.update(mode="pause", note="等待人工验证或核查不确定回执",
                                    delay_seconds=0, decision_source="runtime_safety")
        # Yield immediately, including when a model requests several tools in one
        # response. A control action must not require another model/tool round trip.
        if context.activity.get("mode") in {"continue", "finish", "pause"}:
            raise ActivityYield()
        return result


def build_will_conversation(memory_context, candidate):
    import prompt_builder
    conversation = prompt_builder.build_conversation_messages(memory_context)
    conversation.append({"role": "system", "content": prompt_builder.conversation_style_guard(memory_context)})
    conversation.append({"role": "system", "content": intention_prompt(candidate)})
    # DeepSeek uses a user message to start a fresh thinking/tool-call turn.
    # A system-only trigger leaves the last historical assistant in the current
    # turn, where its absent reasoning_content is rejected before any tool call.
    # This protocol boundary is internal: it is never recorded as human speech.
    conversation.append({
        "role": "user",
        "content": (
            "[Internal Scheduler Trigger] 开始处理上述自主意图。"
            "这是系统调度生成的轮次边界，不是管理员或其他用户发来了新消息。"
            "按本轮意图和权限决定行动，也可以保持安静；不要回应或复述这条内部触发。"
        ),
    })
    return conversation


def run_intention(model, runner, memory_context, candidate, check, *, source="QQ"):
    from tools.context import CURRENT_CONTEXT
    context = CURRENT_CONTEXT.get()
    execution_candidate = candidate
    if (candidate.get("activity") or {}).get("mode") == "review":
        from common.activity_deliberation import choose_activity
        reason = check()
        if reason:
            raise RuntimeError(f"自主行动已取消：{reason}")
        choice = choose_activity(model, memory_context, candidate)
        context.activity.update(
            next_intent=choice["next_intent"] or candidate["intent"],
            topic=choice["topic"], reason=choice["reason"], note=choice["reason"],
            delay_seconds=choice["delay_seconds"], decision_source="model", decision=choice,
        )
        execution_candidate = {**candidate, "intent": choice["next_intent"] or candidate["intent"],
                               "topic": choice["topic"] or candidate["topic"], "reason": choice["reason"]}
        if context.journal:
            context.journal.candidate = dict(execution_candidate)
        log.info("自主选择 · %s · %s · %s", choice["mode"], choice["topic"], choice["reason"])
        if choice["mode"] != "continue":
            context.activity["mode"] = choice["mode"]
            if context.journal:
                context.journal.turn_result = choice["reason"]
            return choice["reason"], []
    guarded = WillSkillRunner(runner, execution_candidate, check, desktop=source != "QQ")
    try:
        text, _, _ = model.run_skill_protocol(
            build_will_conversation(memory_context, execution_candidate), guarded, source=source,
            temperature=0.85, max_iterations=guarded.config.quantum_actions + 1,
        )
    except ActivityYield:
        text = context.activity.get("note", "")
    except RuntimeError as exc:
        if "轮次上限" not in str(exc) or not guarded.outcomes:
            context.activity["observations"] = list(guarded.outcomes)
            raise
        context.activity.update(mode="review", note="工具轮次到达边界，自主考虑下一步",
                                delay_seconds=context.activity.get("delay_seconds", 2),
                                decision_source="runtime")
        text = ""
    except Exception:
        context.activity["observations"] = list(guarded.outcomes)
        raise
    if text and text.startswith("[[SKILL_CALL]]"):
        raise RuntimeError("自主行动轮次未完成，已保留实际操作记录")
    from common.activity import complete_activity
    complete_activity(context, execution_candidate, guarded.outcomes, text)
    if context.activity.get("mode"):
        context.activity["observations"] = list(guarded.outcomes)
    journal = context.journal
    if journal:
        journal.turn_result = text or ""
    return text or "", guarded.outcomes
