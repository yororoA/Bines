from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PersonaState:
    user_name: str = ""
    speaking_habits: list[str] = field(default_factory=list)
    long_term_preferences: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_name": self.user_name,
            "speaking_habits": self.speaking_habits,
            "long_term_preferences": self.long_term_preferences,
            "tech_stack": self.tech_stack,
            "user_preferences": self.user_preferences,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonaState:
        return cls(
            user_name=data.get("user_name", ""),
            speaking_habits=data.get("speaking_habits", []),
            long_term_preferences=data.get("long_term_preferences", []),
            tech_stack=data.get("tech_stack", []),
            user_preferences=data.get("user_preferences", []),
        )

    def to_prompt_string(self) -> str:
        lines: list[str] = []
        if self.user_name:
            lines.append(f"Name: {self.user_name}")
        if self.speaking_habits:
            lines.append(f"Speaking habits: {'; '.join(self.speaking_habits)}")
        if self.long_term_preferences:
            lines.append(f"Preferences: {'; '.join(self.long_term_preferences)}")
        if self.tech_stack:
            lines.append(f"Tech stack: {'; '.join(self.tech_stack)}")
        if self.user_preferences:
            lines.append(f"User preferences: {'; '.join(self.user_preferences)}")
        if not lines:
            return ""
        return "[User Profile]\n" + "\n".join(lines)


class PersonaCache:
    def __init__(self):
        self._cached: dict[str, Any] | None = None
        self._version: int = 0
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any] | None:
        with self._lock:
            return self._cached

    def put(self, snapshot: dict[str, Any]):
        with self._lock:
            self._cached = snapshot
            self._version += 1

    def invalidate(self):
        with self._lock:
            self._cached = None
            self._version += 1

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def has_changed_since(self, old_version: int) -> bool:
        with self._lock:
            return self._version != old_version


persona_cache = PersonaCache()


@dataclass
class PersonaMood:
    arousal: float = 0.0
    consecutive_triggers: int = 0

    @staticmethod
    def arousal_level(arousal: float) -> str:
        if arousal <= 30:
            return "base"
        elif arousal <= 60:
            return "slight"
        elif arousal <= 85:
            return "moderate"
        else:
            return "high"

    def clamp(self):
        self.arousal = max(0.0, min(100.0, self.arousal))
        return self

    def to_dict(self) -> dict:
        return {
            "arousal": self.arousal,
            "consecutive_triggers": self.consecutive_triggers,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> PersonaMood:
        if not data:
            return cls()
        return cls(
            arousal=float(data.get("arousal", 0)),
            consecutive_triggers=int(data.get("consecutive_triggers", 0)),
        ).clamp()