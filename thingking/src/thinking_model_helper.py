"""Single-model client and text-protocol local Skill execution."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from config import DEEPSEEK_MODEL
from common.deepseek_client import get_deepseek_client
from skill_runner import SkillProtocolError, SkillRunner


class ThinkingModelHelper:
    """Shared DeepSeek configuration for plain-text completion and local Skills."""

    def __init__(self):
        self.model = DEEPSEEK_MODEL
        self.client = get_deepseek_client()

    def complete_text(self, messages: List[Dict[str, Any]], *, temperature=0.4, max_tokens=2048) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if not response.choices:
            return ""
        return (response.choices[0].message.content or "").strip()

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
        conversation = [dict(message) for message in messages]
        skill_rules = skill_runner.instructions_for_source(source, allowed_skills)
        if not conversation or conversation[0].get("role") != "system":
            conversation.insert(0, {"role": "system", "content": skill_rules})
        else:
            conversation[0]["content"] = f"{conversation[0].get('content', '')}\n\n{skill_rules}"

        history: List[Dict[str, str]] = []
        final_text = ""
        for index in range(max_iterations):
            final_text = self.complete_text(conversation, temperature=temperature, max_tokens=max_tokens)
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
