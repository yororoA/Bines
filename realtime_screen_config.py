import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union

DEFAULT_REALTIME_SCREEN_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "interval_sec": 10,
    "min_change_ratio": 0.15,
    "backend": "see_now",
    "capture_fps": 4,
    "quality": "high",
    "result_ttl_seconds": 90,
}

SCREEN_SIZES = {"low": (798, 450), "standard": (1050, 600), "high": (1260, 720)}


def _number(value, default, minimum, maximum):
    try:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("non-finite configuration")
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_realtime_screen_config(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    try:
        interval_sec = int(data.get("interval_sec", DEFAULT_REALTIME_SCREEN_CONFIG["interval_sec"]))
    except (ValueError, TypeError, OverflowError):
        interval_sec = DEFAULT_REALTIME_SCREEN_CONFIG["interval_sec"]
    interval_sec = max(3, min(120, interval_sec))

    ratio = _number(data.get("min_change_ratio"), 0.15, 0, 1)
    enabled = data.get("enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"true", "1", "yes", "on"}
    backend = str(data.get("backend", "see_now"))
    quality = str(data.get("quality", "high"))

    return {
        "enabled": bool(enabled),
        "interval_sec": interval_sec,
        "min_change_ratio": ratio,
        "backend": backend if backend in ("see_now", "legacy") else "see_now",
        "capture_fps": _number(data.get("capture_fps"), 4, 1, 12),
        "quality": quality if quality in SCREEN_SIZES else "high",
        "result_ttl_seconds": int(_number(data.get("result_ttl_seconds"), 90, 15, 300)),
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
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return normalized
