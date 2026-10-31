"""Memory system configuration. Values come from environment variables with local defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet
from common.env import load_env

load_env()

THINKING_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = THINKING_DIR / "src"
DEFAULT_DATA_DIR = THINKING_DIR / "memory_data"


def _owner_qq_ids_from_env() -> FrozenSet[str]:
    raw = os.environ.get("BINES_OWNER_QQ_IDS", "").strip()
    if raw:
        return frozenset(part.strip() for part in raw.split(",") if part.strip())
    return frozenset()


@dataclass(frozen=True)
class MemorySettings:
    data_dir: Path = DEFAULT_DATA_DIR
    owner_qq_ids: FrozenSet[str] = field(default_factory=frozenset)
    owner_name: str = "管理员"
    working_window: int = 30
    summarize_trigger: int = 40
    summarize_chunk: int = 30
    summarize_idle_seconds: int = 1800
    summarize_idle_min: int = 4
    recall_k: int = 5
    diary_k: int = 2
    profile_k: int = 8
    day_start_hour: int = 4
    job_lease_seconds: int = 300
    job_max_attempts: int = 5
    job_recovery_cycles: int = 2
    job_recovery_seconds: int = 3600
    state_updates: bool = True
    retrieval_candidates: int = 40
    retrieval_rrf_constant: int = 60
    retrieval_max_distance: float = 0.7
    evidence_limit: int = 3
    reranker_model: str = ""
    associations_enabled: bool = True
    retention_enabled: bool = False
    association_candidates: int = 12
    association_model_candidates: int = 6
    association_backfill_batch: int = 16
    graph_max_hops: int = 2
    graph_expansion_enabled: bool = True
    index_batch_size: int = 64
    graph_candidate_limit: int = 40
    raw_protect_k: int = 3
    episode_protect_k: int = 1
    raw_hot_budget: int = 512
    episode_hot_budget: int = 256
    cold_activation_factor: float = .1

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
    if not data_dir.is_absolute():
        data_dir = THINKING_DIR.parent / data_dir
    return MemorySettings(
        data_dir=data_dir,
        owner_qq_ids=_owner_qq_ids_from_env(),
        owner_name=os.environ.get("BINES_OWNER_NAME", "管理员"),
        job_recovery_cycles=max(0, min(4, int(os.environ.get("BINES_MEMORY_RECOVERY_CYCLES", "2")))),
        job_recovery_seconds=max(300, int(os.environ.get("BINES_MEMORY_RECOVERY_SECONDS", "3600"))),
        state_updates=os.environ.get("BINES_MEMORY_STATE_UPDATES", "true").strip().lower() in {"true", "1", "yes", "on"},
        retrieval_candidates=max(10, min(100, int(os.environ.get("BINES_MEMORY_CANDIDATES", "40")))),
        reranker_model=os.environ.get("BINES_MEMORY_RERANKER", "").strip(),
        summarize_idle_seconds=max(60, int(os.environ.get("BINES_MEMORY_IDLE_SUMMARY_SECONDS", "1800"))),
        summarize_idle_min=max(2, min(40, int(os.environ.get("BINES_MEMORY_IDLE_SUMMARY_MIN", "4")))),
        associations_enabled=os.environ.get("BINES_MEMORY_ASSOCIATIONS", "true").lower() in {"true", "1", "yes", "on"},
        graph_expansion_enabled=os.environ.get("BINES_MEMORY_GRAPH_EXPANSION", "true").lower() in {"true", "1", "yes", "on"},
        index_batch_size=max(1, min(512, int(os.environ.get("BINES_MEMORY_INDEX_BATCH_SIZE", "64")))),
        retention_enabled=os.environ.get("BINES_MEMORY_RETENTION", "false").lower() in {"true", "1", "yes", "on"},
        association_candidates=max(1, min(40, int(os.environ.get("BINES_MEMORY_ASSOCIATION_CANDIDATES", "12")))),
        association_model_candidates=max(1, min(12, int(os.environ.get("BINES_MEMORY_ASSOCIATION_MODEL_CANDIDATES", "6")))),
        association_backfill_batch=max(1, min(200, int(os.environ.get("BINES_MEMORY_BACKFILL_BATCH", "16")))),
        raw_hot_budget=max(0, int(os.environ.get("BINES_MEMORY_RAW_HOT_BUDGET", "512"))),
        episode_hot_budget=max(0, int(os.environ.get("BINES_MEMORY_EPISODE_HOT_BUDGET", "256"))),
    )
