"""Persistent waiting activities; a policy still owns one execution lease.

No model calls here. Waking an activity permits a fresh review, never replays
stored actions. The full checkpoints stay persisted; prompts receive cards only.
"""
import hashlib
import json


def identity(activity):
    return activity.get("id") or "legacy:" + hashlib.sha256(json.dumps(
        {k: activity.get(k) for k in ("kind", "topic", "intent", "started_at")},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def put(waiting, activity):
    if not activity or activity.get("mode") == "finish":
        return
    key = identity(activity)
    prior = waiting.get(key) or {}
    if int(prior.get("sequence") or 0) > int(activity.get("sequence") or 0):
        return
    waiting[key] = {**activity, "id": key}


def adopt(state, checkpoint):
    """Merge a handoff/result by identity instead of replacing unrelated goals."""
    checkpoint = dict(checkpoint)
    current = state.activity
    key = checkpoint.get("id") or (identity(current) if current else identity(checkpoint))
    checkpoint["id"] = key
    prior = state.waiting_activities.get(key) or (current if current and identity(current) == key else {})
    if "sequence" in checkpoint and int(prior.get("sequence") or 0) > int(checkpoint.get("sequence") or 0):
        return False
    if checkpoint.get("mode") == "finish":
        state.waiting_activities.pop(key, None)
        if current and identity(current) == key:
            state.activity = {}
        return True
    if current and identity(current) != key:
        put(state.waiting_activities, current)
    state.waiting_activities.pop(key, None)
    state.activity = checkpoint
    return True


def advance(state, now, interval):
    """Timed waits release the focus; due goals rotate fairly between quanta."""
    if state.pending:
        return  # Never change focus while a worker owns the candidate.
    current = state.activity
    # Zero-delay pauses are manual/uncertain-result barriers, not timers.
    if current.get("mode") == "pause" and not current.get("delay_seconds"):
        return
    due_waiting = any(now >= float(a.get("next_at") or 0)
                      and not (a.get("mode") == "pause" and not a.get("delay_seconds"))
                      for a in state.waiting_activities.values())
    if (current.get("mode") in {"continue", "review"} and not due_waiting
            and float(current.get("next_at") or 0) > now):
        # Short playback/tool gaps keep the current quantum cadence. Longer
        # waits allow another intention to be considered in the meantime.
        if float(current["next_at"]) - now <= interval:
            return
    parked = False
    if current.get("mode") in {"pause", "continue", "review"}:
        parked = current.get("mode") == "pause" or float(current.get("next_at") or 0) > now
        put(state.waiting_activities, current)
        state.activity = {}
    ready = [a for a in state.waiting_activities.values()
             if a.get("mode") in {"continue", "review", "pause"}
             and not (a.get("mode") == "pause" and not a.get("delay_seconds"))
             and now >= float(a.get("next_at") or 0)]
    if ready:
        chosen = min(ready, key=lambda a: (float(a.get("next_at") or 0),
                                           float(a.get("updated_at") or 0), identity(a)))
        state.waiting_activities.pop(identity(chosen))
        # A different activity may have used the browser in the meantime.
        # Re-observe/review instead of blindly continuing old tool arguments.
        switched = not current or identity(chosen) != identity(current) or current.get("mode") == "pause"
        state.activity = {**chosen, "mode": "review"} if switched else chosen
        if switched:
            state.next_decision = min(state.next_decision, float(chosen.get("next_at") or now))
    elif parked:
        state.next_decision = min(state.next_decision, now + interval)


def cards(state, limit=8):
    rows = sorted(state.waiting_activities.values(),
                  key=lambda a: (float(a.get("next_at") or 0), identity(a)))
    return [{k: a.get(k) for k in ("id", "kind", "topic", "intent", "mode", "next_at")}
            for a in rows[:limit]]
