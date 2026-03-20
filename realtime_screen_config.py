import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

DEFAULT_REALTIME_SCREEN_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "interval_sec": 10,
    "min_change_ratio": 0.15,
}


def normalize_realtime_screen_config(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = data or {}
    try:
        interval_sec = int(data.get("interval_sec", DEFAULT_REALTIME_SCREEN_CONFIG["interval_sec"]))
    except Exception:
        interval_sec = DEFAULT_REALTIME_SCREEN_CONFIG["interval_sec"]
    interval_sec = max(3, min(120, interval_sec))

    try:
        ratio = float(data.get("min_change_ratio", DEFAULT_REALTIME_SCREEN_CONFIG["min_change_ratio"]))
    except Exception:
        ratio = DEFAULT_REALTIME_SCREEN_CONFIG["min_change_ratio"]
    ratio = max(0.0, min(1.0, ratio))

    return {
        "enabled": bool(data.get("enabled", DEFAULT_REALTIME_SCREEN_CONFIG["enabled"])),
        "interval_sec": interval_sec,
        "min_change_ratio": ratio,
    }


def load_realtime_screen_config(config_path: Union[str, Path]) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        return dict(DEFAULT_REALTIME_SCREEN_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return normalize_realtime_screen_config(data)
    except Exception:
        return dict(DEFAULT_REALTIME_SCREEN_CONFIG)


def save_realtime_screen_config(config_path: Union[str, Path], data: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_realtime_screen_config(data)
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    return normalized
