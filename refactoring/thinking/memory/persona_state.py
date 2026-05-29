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
        return self._version


persona_cache = PersonaCache()