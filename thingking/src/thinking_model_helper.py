"""Single-model client and text-protocol local Skill execution."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Tuple

from config import DEEPSEEK_MODEL
from common.deepseek_client import get_deepseek_client
from common.clock import with_current_time
from skill_runner import SkillProtocolError, SkillRunner

logger = logging.getLogger("bines.thinking.model")
_MAX_TEXT_RETRY_TOKENS = 262144


def _confirm_memory_request(messages):
    from tools.context import CURRENT_CONTEXT
    journal = CURRENT_CONTEXT.get().journal
    if journal and hasattr(journal, "confirm_memory_presentations"):
        journal.confirm_memory_presentations(messages)


class ModelCompletionError(RuntimeError):
    """The API succeeded but did not return a complete final answer."""


class ThinkingModelHelper:
    """Shared DeepSeek configuration for plain-text completion and local Skills."""

    def __init__(self):
        self.model = DEEPSEEK_MODEL
        self.client = get_deepseek_client()

    def complete_text(self, messages: List[Dict[str, Any]], *, temperature=0.4, max_tokens=2048,
                      response_format: Dict[str, str] | None = None, retry_tokens: int | None = None,
                      thinking: str | None = None, timeout: float | None = None,
                      max_retries: int | None = None) -> str:
        # Thinking and final text share the output budget. Retry the original
        # request once with more room; never use reasoning or truncated text.
        client = self.client
        if max_retries is not None:
            from common.deepseek_client import DeepSeekClient
            client = DeepSeekClient(self.client.with_options(max_retries=max_retries))
        retry_budget = _MAX_TEXT_RETRY_TOKENS if retry_tokens is None else retry_tokens
        for attempt in range(2):
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                temperature=temperature,
                max_tokens=max_tokens,
                **({"response_format": response_format} if response_format else {}),
                **({"extra_body": {"thinking": {"type": thinking}}} if thinking else {}),
                **({"timeout": timeout} if timeout is not None else {}),
            )
            if not response.choices:
                raise ModelCompletionError(f"model {self.model} returned no choices")
            _confirm_memory_request(messages)
            choice = response.choices[0]
            text = (choice.message.content or "").strip()
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason == "length":
                if attempt == 0 and retry_budget > max_tokens:
                    logger.warning("model %s reached output limit %d; retrying with %d tokens",
                                   self.model, max_tokens, retry_budget)
                    max_tokens = retry_budget
                    continue
            if finish_reason == "length" or not text:
                usage = getattr(response, "usage", None)
                details = getattr(usage, "completion_tokens_details", None)
                raise ModelCompletionError(
                    f"model {self.model} returned {'truncated output' if finish_reason == 'length' else 'empty text'}"
                    f" (finish_reason={finish_reason}, max_tokens={max_tokens},"
                    f" completion_tokens={getattr(usage, 'completion_tokens', None)},"
                    f" reasoning_tokens={getattr(details, 'reasoning_tokens', None)})"
                )
            return text

    def run_skill_protocol(
        self,
        messages: List[Dict[str, Any]],
        skill_runner: SkillRunner,
        *,
        source: str | None = None,
        allowed_skills: Iterable[str] | None = None,
        max_iterations: int = 8,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> Tuple[str, List[Dict[str, str]], List[str]]:
        """Request one text-protocol skill at a time and return ordinary final text."""
        if source in {"QQ", "qq"}:
            return self.run_tool_calls(messages, skill_runner, source=source,
                                       max_iterations=max(24, max_iterations),
                                       temperature=temperature, max_tokens=max_tokens,
                                       allowed_skills=allowed_skills)
        conversation = [dict(message) for message in messages]
        skill_rules = skill_runner.instructions_for_source(source, allowed_skills)
        if not conversation or conversation[0].get("role") != "system":
            conversation.insert(0, {"role": "system", "content": skill_rules})
        else:
            conversation[0]["content"] = f"{conversation[0].get('content', '')}\n\n{skill_rules}"

        history: List[Dict[str, str]] = []
        final_text = ""
        for index in range(max_iterations):
            final_text = self.complete_text(with_current_time(conversation),
                                            temperature=temperature, max_tokens=max_tokens)
            try:
                command = skill_runner.parse_model_command(final_text)
            except SkillProtocolError as exc:
                command = {"skill": "", "action": "", "args": {}}
                result = f"技能协议错误：{exc}。请按协议重试。"
            else:
                if command is None:
                    return final_text, history, []
                result = skill_runner.execute(
                    command,
                    source=source,
                    allowed_skills=allowed_skills,
                )
                history.append({
                    "skill": command["skill"],
                    "action": command["action"],
                    "result": result,
                })

            conversation.append({"role": "assistant", "content": final_text})
            conversation.append({
                "role": "system",
                "content": f"<skill_result>\n{result}\n</skill_result>\n根据结果继续执行所需技能，或用普通自然语言回答。",
            })

        return final_text or (history[-1]["result"] if history else ""), history, []

    def run_tool_calls(self, messages, skill_runner, *, source="QQ", max_iterations=24,
                       temperature=0.2, max_tokens=2048, allowed_skills=None):
        """Execute model-selected native calls in order, preserving thinking turns."""
        conversation = [dict(message) for message in messages]
        rules = skill_runner.instructions_for_source(source, allowed_skills, native=True)
        # Tool documentation describes capabilities; the following character and
        # scoped context define how the assistant speaks and what it remembers.
        conversation.insert(0, {"role": "system", "content": rules})
        tools = skill_runner.function_tools(source, allowed_skills)
        # Keep the turn's already-sampled voice after tool results, which otherwise
        # become the last instructions before each newly generated message.
        voice = next((message["content"] for message in reversed(messages)
                      if message.get("role") == "system"
                      and message.get("content", "").startswith("--- 本轮角色口吻")), "")
        history = []
        for _ in range(max_iterations):
            request_messages = [*conversation, *([{"role": "system", "content": voice}] if voice else [])]
            request_messages = with_current_time(request_messages)
            budget = max_tokens
            for attempt in range(2):
                response = self.client.chat.completions.create(
                    model=self.model, messages=request_messages, tools=tools or None,
                    temperature=temperature, max_tokens=budget, stream=False,
                )
                if not response.choices:
                    raise ModelCompletionError(f"model {self.model} returned no choices")
                _confirm_memory_request(request_messages)
                choice = response.choices[0]
                if getattr(choice, "finish_reason", None) != "length":
                    break
                # No tool from a truncated response has run yet. Retry only this
                # request, preserving all receipts/results from earlier rounds.
                if attempt or budget >= _MAX_TEXT_RETRY_TOKENS:
                    raise ModelCompletionError("QQ 工具响应被截断，未执行本次返回的工具")
                budget = min(_MAX_TEXT_RETRY_TOKENS, max(8192, budget * 4))
            message = response.choices[0].message
            calls = message.tool_calls or []
            if not calls:
                if not (message.content or "").strip():
                    raise ModelCompletionError("QQ 模型没有返回正文或工具调用")
                return (message.content or "").strip(), history, []
            assistant = {"role": "assistant", "content": message.content or "",
                         "tool_calls": [call.model_dump(exclude_none=True) for call in calls]}
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning is not None:
                assistant["reasoning_content"] = reasoning
            conversation.append(assistant)
            for call in calls:
                action = call.function.name
                try:
                    args = json.loads(call.function.arguments)
                except (TypeError, json.JSONDecodeError):
                    result = "工具参数必须是有效的 JSON 对象"
                else:
                    result = skill_runner.execute(
                        {"skill": skill_runner.action_to_skill.get(action),
                         "action": action, "args": args}, source=source,
                        allowed_skills=allowed_skills,
                    )
                history.append({"action": action, "result": result})
                conversation.append({"role": "tool", "tool_call_id": call.id, "content": result})
        raise RuntimeError("QQ 工具调用达到轮次上限；已执行的操作保留回执")
