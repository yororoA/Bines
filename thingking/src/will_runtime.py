"""Execute one autonomous intention through the existing tool runner."""
import json
import os
import re
import subprocess
import ast

from common.proactive import intention_prompt

READ_ACTIONS = {
    "get_time", "search_memory", "browser_search", "web_fetch",
    "napcat_discover", "napcat_catalog", "napcat_events", "napcat_operation", "napcat_custom_faces",
    "get_screen_info", "get_visual_info", "fast_screen_analysis", "template_match", "find_color_region",
    "get_qq_group_list", "get_qq_friend_list", "get_moments", "get_comments",
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
}


def outcome_succeeded(item):
    result = item.get("result", "")
    if result.startswith(("技能执行失败", "自主行动未执行", "Error:", "错误", "无法", "未找到")):
        return False
    try:
        payload = ast.literal_eval(result)
    except (ValueError, SyntaxError):
        return bool(result)
    if isinstance(payload, dict) and "status" in payload:
        return payload["status"] == "ok" and payload.get("retcode", 0) == 0
    return True


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
    def __init__(self, runner, candidate, check, *, desktop=False):
        self.runner, self.candidate, self.check = runner, candidate, check
        self.desktop = desktop
        self.outcomes = []
        self.last_desktop_action = 0
        self.called = 0

    def __getattr__(self, name):
        return getattr(self.runner, name)

    def execute(self, command, **kwargs):
        self.called += 1
        if self.called > 8:
            return "自主行动未执行：本轮操作额度已用完，请结束"
        reason = self.check()
        if reason:
            raise RuntimeError(f"自主行动已取消：{reason}")
        action = command.get("action")
        if not self.candidate.get("contact_allowed") and action in CONTACT_ACTIONS | {"mcp_call"}:
            return "自主行动未执行：当前只允许不打扰他人的查询或观察"
        if action in DESKTOP_ACTIONS:
            import time
            idle = desktop_idle_seconds() if self.desktop else None
            last_input = time.time() - idle if idle is not None else time.time()
            owned_input = self.last_desktop_action and last_input <= self.last_desktop_action + 0.5
            if idle is None or (idle < 60 and not owned_input):
                return "自主行动未执行：电脑正在使用或无法确认键鼠空闲"
        result = self.runner.execute(command, **kwargs)
        if action in DESKTOP_ACTIONS:
            self.last_desktop_action = time.time()
        self.outcomes.append({"action": action, "result": str(result)[:2000]})
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
    guarded = WillSkillRunner(runner, candidate, check, desktop=source != "QQ")
    text, _, _ = model.run_skill_protocol(
        build_will_conversation(memory_context, candidate), guarded, source=source,
        temperature=0.85, max_iterations=8,
    )
    if text and text.startswith("[[SKILL_CALL]]"):
        raise RuntimeError("自主行动轮次未完成，已保留实际操作记录")
    return text or "", guarded.outcomes
