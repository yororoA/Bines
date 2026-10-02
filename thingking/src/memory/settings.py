"""Memory system configuration. Values come from environment variables with local defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet

THINKING_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = THINKING_DIR / "src"
DEFAULT_DATA_DIR = THINKING_DIR / "memory_data"


def _owner_qq_ids_from_env() -> FrozenSet[str]:
    raw = os.environ.get("BINES_OWNER_QQ_IDS", "").strip()
    if raw:
        return frozenset(part.strip() for part in raw.split(",") if part.strip())
    try:
        from chatBot.config import ADMIN_QQ_LIST
    except Exception:
        return frozenset()
    return frozenset(str(qq) for qq in ADMIN_QQ_LIST)


@dataclass(frozen=True)
class MemorySettings:
    data_dir: Path = DEFAULT_DATA_DIR
    owner_qq_ids: FrozenSet[str] = field(default_factory=frozenset)
    owner_name: str = "管理员"
    working_window: int = 30
    summarize_trigger: int = 40
    summarize_chunk: int = 30
    recall_k: int = 5
    diary_k: int = 2
    profile_k: int = 8
    day_start_hour: int = 4
    job_lease_seconds: int = 300
    job_max_attempts: int = 5

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "legacy_backup"


def load_settings() -> MemorySettings:
    data_dir = Path(os.environ.get("BINES_MEMORY_DIR", str(DEFAULT_DATA_DIR)))
    return MemorySettings(
        data_dir=data_dir,
        owner_qq_ids=_owner_qq_ids_from_env(),
        owner_name=os.environ.get("BINES_OWNER_NAME", "管理员"),
    )
