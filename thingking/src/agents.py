"""Pure-Python DeepSeek agents using the local SKILL.md text protocol."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import DEEPSEEK_MODEL
from common.deepseek_client import get_deepseek_client
from thinking_model_helper import ThinkingModelHelper
from skill_runner import SkillRunner


SKILL_PROTOCOL = """需要执行本地操作时，整条回复只能是一个技能调用块，不要添加解释：
[[SKILL_CALL]]
{"skill":"技能名","action":"动作名","args":{}}
[[/SKILL_CALL]]
普通自然语言回复表示本轮已完成。收到技能结果后可以继续请求下一个技能。"""


def _clean_legacy_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove old OpenAI tool-call structures before they reach the text-only API path."""
    cleaned = []
    for original in messages or []:
        message = dict(original)
        if message.get("role") == "assistant" and message.get("tool_calls"):
            message.pop("tool_calls", None)
            if not message.get("content"):
                continue
        if message.get("role") == "tool":
            message = {
                "role": "system",
                "content": f"[旧版工具结果] {message.get('content', '')}",
            }
        cleaned.append(message)
    return cleaned


class MainAgent:
    """Primary conversation agent; model-native function calling is disabled."""

    def __init__(self):
        self.model = DEEPSEEK_MODEL
        self.client = get_deepseek_client()
        self.skill_instructions = ""

    def set_skill_instructions(self, instructions: str) -> None:
        self.skill_instructions = instructions or ""

    def get_system_prompt(self) -> str:
        base = """你是一个有鲜明人设的情感对话助手。理解用户意图并自然回应，保持角色语气。
- 用户问过去的对话、记忆或聊天记录时，先从已提供的上下文中回答，不要调用屏幕或浏览器动作。
- 用户明确要求实际操作时，必须使用已启用的本地技能；不要编造执行结果。
- 不输出括号内的动作描述。若无需回复则保持空内容。
- 用户要求写代码、调试或解释技术问题时，应直接给出有帮助的回答。
"""
        sections = [base, SKILL_PROTOCOL]
        if self.skill_instructions:
            sections.append(self.skill_instructions)
        return "\n\n".join(sections)

    def _post_chat_completion(self, payload: Dict[str, Any], stream: bool = False):
        request = dict(payload)
        request.pop("stream", None)
        return self.client.chat.completions.create(**request, stream=stream)

    def _ensure_system_prompt(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{"role": "system", "content": self.get_system_prompt()}] + _clean_legacy_messages(messages)

    def run_one_turn_streaming_text_only(self, messages: List[Dict], interrupt_check=None):
        holder: Dict[str, Any] = {}
        payload = {
            "messages": self._ensure_system_prompt(messages),
            "model": self.model,
            "frequency_penalty": 0,
            "max_tokens": 4096,
            "presence_penalty": 0,
            "stream": True,
            "temperature": 0.7,
            "top_p": 1,
        }
        try:
            response = self._post_chat_completion(payload, stream=True)
        except Exception as exc:
            holder["error"] = str(exc)
            return iter(()), holder

        full_content = []

        def generate():
            try:
                for chunk in response:
                    if interrupt_check and interrupt_check():
                        holder["interrupted"] = True
                        return
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    content = getattr(choices[0].delta, "content", None)
                    if content:
                        full_content.append(content)
                        yield content
            except Exception as exc:
                holder["error"] = str(exc)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                content = "".join(full_content)
                holder["message"] = {"role": "assistant", "content": content or None}

        return generate(), holder

    def run_one_turn_streaming(self, messages: List[Dict], interrupt_check=None):
        return self.run_one_turn_streaming_text_only(messages, interrupt_check=interrupt_check)

    def handle(self, user_input: str, context_messages: List[Dict]) -> Dict[str, Any]:
        messages = _clean_legacy_messages(context_messages)
        messages.append({"role": "user", "content": user_input})
        try:
            response = self._post_chat_completion({
                "messages": self._ensure_system_prompt(messages),
                "model": self.model,
                "temperature": 0.7,
                "max_tokens": 4096,
                "stream": False,
            })
            content = response.choices[0].message.content or ""
            return {"need_tool": False, "need_state_update": False, "reply": content}
        except Exception as exc:
            return {"need_tool": False, "need_state_update": False, "reply": f"抱歉，处理时出现错误：{exc}"}

    def reply_with_result(self, user_input: str, tool_result: str, state_result: Optional[str] = None) -> str:
        content = f"用户说：{user_input}\n\n技能执行结果：{tool_result}"
        if state_result:
            content += f"\n\n状态更新结果：{state_result}"
        try:
            response = self._post_chat_completion({
                "messages": [
                    {"role": "system", "content": "基于技能执行结果生成自然、准确的角色回复。不要虚构结果。"},
                    {"role": "user", "content": content},
                ],
                "model": self.model,
                "temperature": 0.7,
                "max_tokens": 2048,
                "stream": False,
            })
            return response.choices[0].message.content or "任务已完成。"
        except Exception:
            return f"任务已完成：{tool_result}"


class SummaryAgent:
    """Summary and state-maintenance prompts use the same DeepSeek model."""

    def __init__(self, skill_runner: SkillRunner | None = None):
        self.thinking_helper = ThinkingModelHelper()
        self.skill_runner = skill_runner

    def set_skill_runner(self, skill_runner: SkillRunner) -> None:
        self.skill_runner = skill_runner

    def update_state(self, state_update_description: str, context: str, base_messages: List[Dict]) -> str:
        if self.skill_runner is None:
            return "本地技能运行器尚未初始化。"
        messages = _clean_legacy_messages(base_messages)
        messages.extend([
            {"role": "system", "content": "根据对话事实维护动态状态。只更新明确发生变化的字段；无变化时直接回复无需更新。"},
            {"role": "user", "content": f"状态变化描述：{state_update_description}\n上下文：{context or '无'}"},
        ])
        try:
            result, _, _ = self.thinking_helper.run_skill_protocol(
                messages, self.skill_runner, allowed_skills={"memory-maintenance"},
                max_iterations=3, temperature=0.0, max_tokens=1200,
            )
            return result or "状态更新完成。"
        except Exception as exc:
            return f"状态更新失败：{exc}"

    def summarize_content(self, content: str) -> str:
        if not content:
            return ""
        try:
            return self.thinking_helper.complete_text([
                {"role": "system", "content": "你是对话摘要助手。准确压缩重要话题、事实、参与者和观点，忽略无关寒暄。"},
                {"role": "user", "content": f"请总结以下内容：\n\n{content}"},
            ], temperature=0.3, max_tokens=1024)
        except Exception as exc:
            return f"摘要失败：{exc}"


class DynamicMemoryMaintainer:
    """Update dynamic memory through the memory-maintenance Skill."""

    def __init__(self, skill_runner: SkillRunner | None = None):
        self.thinking_helper = ThinkingModelHelper()
        self.skill_runner = skill_runner

    def update_dynamic_memory_from_summary(self, summary_text: str, current_dynamic_json: str) -> str:
        if not self.skill_runner or not summary_text.strip():
            return ""
        messages = [
            {"role": "system", "content": "根据摘要更新角色的动态记忆，只写入有明确证据的变化。关系变化必须使用整数 relationship_delta。无变化时直接说明无需更新。"},
            {"role": "user", "content": f"当前状态：\n{current_dynamic_json}\n\n摘要：\n{summary_text.strip()}"},
        ]
        try:
            result, _, _ = self.thinking_helper.run_skill_protocol(
                messages, self.skill_runner, allowed_skills={"memory-maintenance"},
                max_iterations=3, temperature=0.0, max_tokens=1200,
            )
            return result or ""
        except Exception as exc:
            return f"动态记忆更新失败：{exc}"
