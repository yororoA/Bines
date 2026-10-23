"""Persistent activity checkpoints and owner controls shared by QQ and desktop."""
import json
import os
from pathlib import Path
import time

from common.website_spool import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]


def control_path():
    directory = Path(os.getenv("BINES_MEMORY_DIR", str(ROOT / "thingking" / "memory_data")))
    if not directory.is_absolute():
        directory = ROOT / directory
    return directory / "autonomy_control.json"


def activity_control():
    try:
        data = json.loads(control_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_owner_control(mode, reason=""):
    if mode not in {"pause", "resume", "stop"}:
        raise ValueError("mode 必须是 pause、resume 或 stop")
    data = {"mode": mode, "reason": str(reason)[:500], "updated_at": time.time()}
    atomic_write_json(control_path(), data)
    return data


def activity_checkpoint(candidate, control, outcomes, text=""):
    """Only bounded observations survive the next quantum; never replay tool calls."""
    old = candidate.get("activity") or {}
    outcomes = outcomes or control.get("observations") or []
    observations = [
        {"action": row.get("action"), "args": row.get("args") or {},
         "result": str(row.get("result") or "")[:1800], "status": row.get("status")}
        for row in outcomes[-10:]
    ]
    return {
        "id": old.get("id") or candidate.get("id"),
        "sequence": int(old.get("sequence") or 0) + 1,
        "mode": control.get("mode", "finish"),
        "intent": str(control.get("next_intent") or candidate["intent"])[:500],
        "note": str(control.get("note") or text)[:2000],
        "delay_seconds": max(0, min(3600, float(control.get("delay_seconds", 2)))),
        "observations": observations,
        "started_at": old.get("started_at") or candidate.get("timestamp") or time.time(),
    }
