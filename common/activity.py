"""Persistent activity checkpoints and owner controls shared by QQ and desktop."""
import json
import os
from pathlib import Path
import re
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
    from common.memory_journal import result_data
    def bounded_result(value):
        data = result_data(value)
        if isinstance(data, dict):
            data = {key: item for key, item in data.items() if key not in {"snapshot", "output"}}
            if "analysis" in data:
                data = {key: data[key] for key in
                        ("status", "source_url", "audio_observed", "observation_scope", "analysis",
                         "page_changed", "playback_advanced", "frames") if key in data}
            # Keep physical page/media state ahead of long accessibility trees.
            return json.dumps(data, ensure_ascii=False)[:4000]
        return str(value or "")[:1800]
    observations = [
        {"action": row.get("action"), "args": row.get("args") or {},
         "result": bounded_result(row.get("result")), "status": row.get("status")}
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
        "idle_batches": int(control.get("idle_batches") or 0),
        "topic": str(control.get("topic") or old.get("topic") or candidate.get("topic") or "")[:80],
        "reason": str(control.get("reason") or old.get("reason") or candidate.get("reason") or "")[:500],
        "decision_source": control.get("decision_source", "runtime"),
        "decision": control.get("decision") or old.get("decision") or {},
    }


def ongoing_request(text):
    """Recognize an owner's open-ended request; never infer it from website text."""
    return bool(re.search(
        r"持续|一直|接着(?:刷|看|浏览|搜)|继续(?:刷|浏览|探索|活动)|"
        r"刷去|随便你刷|自己(?:刷|逛|浏览)|慢慢(?:刷|逛|看)|不停地",
        str(text or ""),
    ))


def complete_activity(context, candidate, outcomes, text="", *, ordinary=False):
    """An undecided boundary asks the model; it does not choose continuation."""
    if context.activity.get("mode"):
        return
    if ordinary and (candidate.get("activity") or {}).get("mode") == "pause":
        # A casual reply does not revoke a timed/manual pause. An explicit
        # activity tool above may still update it at the owner's request.
        return
    browsing = any(row.get("action") in {
        "website_open", "website_snapshot", "website_scroll", "website_interact",
        "website_media_status", "website_watch", "website_tabs", "browser_search", "web_fetch",
    } for row in outcomes)
    needs_choice = (candidate.get("activity") or
                    (ongoing_request(context.user_text) or browsing if ordinary
                     else candidate.get("kind") != "connection"))
    if not needs_choice:
        return
    # The former progress->continue / no-progress->fixed-pause rule delegated
    # interest to the program. Keep the observation and let Bines choose instead.
    context.activity.update(
        mode="review", next_intent=context.activity.get("next_intent") or candidate["intent"],
        note=(text or "本轮已结束；根据真实观察自主选择接下来想做什么")[:2000],
        delay_seconds=context.activity.get("delay_seconds", 2), decision_source="runtime",
    )


def desktop_activity():
    directory = control_path().parent
    def read(name):
        try:
            return json.loads((directory / name).read_text())
        except (OSError, ValueError):
            return {}
    state = read("proactive_desktop.json")
    handoff = read("desktop_activity_handoff.json")
    if handoff.get("id") and handoff["id"] != state.get("handoff_ref"):
        return handoff.get("activity") or {}
    return state.get("activity") or {}


def save_desktop_handoff(turn_id, checkpoint):
    directory = control_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "desktop_activity_handoff.json"
    # The desktop scheduler and conversation process have separate lifetimes.
    # Keep every unacknowledged goal, including across two quick owner turns.
    with (directory / "desktop_activity_handoff.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.write(b"\0"); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            saved = {}
        try:
            acknowledged = json.loads((directory / "proactive_desktop.json").read_text()).get("handoff_ref")
        except (OSError, ValueError):
            acknowledged = None
        if turn_id == acknowledged:
            return
        entries = saved.get("handoffs") or ([{"id": saved["id"], "activity": saved["activity"]}] if saved.get("id") else [])
        for index, entry in enumerate(entries):
            if entry["id"] == acknowledged:
                entries = entries[index + 1:]
                break
        if not any(entry["id"] == turn_id for entry in entries):
            entries.append({"id": turn_id, "activity": checkpoint})
        atomic_write_json(path, {**entries[-1], "handoffs": entries})


def consume_desktop_handoff(policy):
    try:
        handoff = json.loads((control_path().parent / "desktop_activity_handoff.json").read_text())
    except (OSError, ValueError):
        return False
    if policy.state.pending or not handoff.get("id") or handoff["id"] == policy.state.handoff_ref:
        return False
    entries = handoff.get("handoffs") or [handoff]
    for index, entry in enumerate(entries):
        if entry["id"] == policy.state.handoff_ref:
            entries = entries[index + 1:]
            break
    for entry in entries:
        policy.accept_activity(entry["activity"], time.time())
    policy.state.handoff_ref = handoff["id"]
    return True
