import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class RealtimeScreenPaths:
    config: Path
    analysis: Path
    context: Path
    flush_signal: Path


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


def read_realtime_screen_analysis_if_enabled(paths: RealtimeScreenPaths) -> str:
    """启用时读取分析文本；未启用/为空/异常均返回空字符串。"""
    try:
        if not is_realtime_screen_enabled(paths):
            return ""
        if not paths.analysis.exists():
            return ""
        with open(paths.analysis, "r", encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def write_realtime_screen_context_if_enabled(
    paths: RealtimeScreenPaths,
    user_input: str,
    assistant_output: str,
) -> bool:
    """启用时写入 context 文件，返回是否写入成功。"""
    try:
        if not is_realtime_screen_enabled(paths):
            return False
        paths.context.parent.mkdir(parents=True, exist_ok=True)
        with open(paths.context, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "user_input": user_input,
                    "assistant_output": assistant_output,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
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
