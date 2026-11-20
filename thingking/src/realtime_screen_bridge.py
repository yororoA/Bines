import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from common.screen_observation import atomic_write_json, format_observation, read_observation


@dataclass(frozen=True)
class RealtimeScreenPaths:
    config: Path
    analysis: Path
    context: Path
    flush_signal: Path

    @property
    def observation(self) -> Path:
        return self.analysis.with_name("realtime_screen_observation.json")


def build_realtime_screen_paths(project_root: Path) -> RealtimeScreenPaths:
    server_dir = project_root / "server"
    return RealtimeScreenPaths(
        config=server_dir / "realtime_screen_config.json",
        analysis=server_dir / "realtime_screen_analysis.txt",
        context=server_dir / "realtime_screen_context.json",
        flush_signal=server_dir / "realtime_screen_flush.signal",
    )


def is_realtime_screen_enabled(paths: RealtimeScreenPaths) -> bool:
    try:
        if not paths.config.exists():
            return False
        with open(paths.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(cfg.get("enabled"))
    except Exception:
        return False


def read_realtime_screen_observation_if_enabled(paths: RealtimeScreenPaths) -> dict:
    """Read structured metadata; stale/failed/stopped observations have no analysis."""
    try:
        config = json.loads(paths.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not config.get("enabled"):
            return {"status": "disabled", "analysis": "", "fresh": False}
        return read_observation(paths.observation, max_age_seconds=config.get("result_ttl_seconds", 90))
    except (OSError, ValueError, UnicodeError):
        return {"status": "unavailable", "analysis": "", "fresh": False}


def read_realtime_screen_analysis_if_enabled(paths: RealtimeScreenPaths) -> str:
    """Return timestamped external observation text, never the unverified legacy txt."""
    return format_observation(read_realtime_screen_observation_if_enabled(paths))


def write_realtime_screen_context_if_enabled(
    paths: RealtimeScreenPaths,
    user_input: str,
    assistant_output: str,
) -> bool:
    """启用时写入 context 文件，返回是否写入成功。"""
    try:
        if not is_realtime_screen_enabled(paths):
            return False
        atomic_write_json(paths.context, {
            "user_input": user_input,
            "assistant_output": assistant_output,
            "request_id": uuid.uuid4().hex,
            "updated_at": time.time(),
        })
        return True
    except Exception:
        return False


def send_realtime_screen_flush_signal(paths: RealtimeScreenPaths) -> bool:
    try:
        paths.flush_signal.parent.mkdir(parents=True, exist_ok=True)
        paths.flush_signal.touch()
        return True
    except Exception:
        return False
