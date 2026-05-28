from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PersonaState:
    tone: str = "friendly"
    style: str = "concise"
    verbosity: str = "moderate"
    role_identity: str = "assistant"
    speaking_habits: list[str] = field(default_factory=list)
    interaction_strategy: str = "collaborative"
    long_term_preferences: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tone": self.tone,
            "style": self.style,
            "verbosity": self.verbosity,
            "role_identity": self.role_identity,
            "speaking_habits": self.speaking_habits,
            "interaction_strategy": self.interaction_strategy,
            "long_term_preferences": self.long_term_preferences,
            "tech_stack": self.tech_stack,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonaState:
        return cls(
            tone=data.get("tone", "friendly"),
            style=data.get("style", "concise"),
            verbosity=data.get("verbosity", "moderate"),
            role_identity=data.get("role_identity", "assistant"),
            speaking_habits=data.get("speaking_habits", []),
            interaction_strategy=data.get("interaction_strategy", "collaborative"),
            long_term_preferences=data.get("long_term_preferences", []),
            tech_stack=data.get("tech_stack", []),
        )