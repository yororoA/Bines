"""Persistent, deterministic life continuity derived from real Bines activity."""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict

from .models import ORIGIN_LIVE, OWNER_SCOPE, ROLE_ASSISTANT, ROLE_USER, ZONE

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS life_state (
        id TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        version INTEGER NOT NULL,
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS life_events (
        source_ref TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        kind TEXT NOT NULL,
        ts REAL NOT NULL,
        payload TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        recorded_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_life_events_time ON life_events(ts, source_ref)",
    "CREATE INDEX IF NOT EXISTS idx_life_events_scope ON life_events(scope, kind, ts)",
)

_PHASE_LABELS = {
    "sleeping": "睡眠",
    "night_awake": "夜间清醒",
    "waking": "苏醒",
    "active": "清醒活动",
    "winding_down": "准备休息",
}
_TERMINAL = {
    "succeeded": "completed", "completed": "completed", "sent": "completed",
    "failed": "failed", "uncertain": "uncertain", "skipped": "skipped",
    "interrupted": "interrupted", "empty": "completed",
}


@dataclass(frozen=True)
class LifeConfig:
    sleep_start: int = 1
    wake_hour: int = 8
    wake_window_hours: int = 2
    wind_down_hours: int = 2

    @classmethod
    def from_env(cls):
        config = cls(
            sleep_start=int(os.getenv("BINES_LIFE_SLEEP_START",
                                      os.getenv("BINES_PROACTIVE_QUIET_START", "1"))),
            wake_hour=int(os.getenv("BINES_LIFE_WAKE_HOUR",
                                    os.getenv("BINES_PROACTIVE_QUIET_END", "8"))),
            wake_window_hours=int(os.getenv("BINES_LIFE_WAKE_WINDOW", "2")),
            wind_down_hours=int(os.getenv("BINES_LIFE_WIND_DOWN", "2")),
        )
        if any(not 0 <= value < 24 for value in (config.sleep_start, config.wake_hour)):
            raise ValueError("生命作息小时必须在 0 到 23 之间")
        if not 1 <= config.wake_window_hours <= 6 or not 1 <= config.wind_down_hours <= 6:
            raise ValueError("苏醒与准备休息窗口必须在 1 到 6 小时之间")
        return config


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: Any, default: float, low: float = 0, high: float = 1) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, value)) if math.isfinite(value) else default


def _inside(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def _phase(now: float, last_human_at: float = 0, config: LifeConfig | None = None) -> str:
    config = config or LifeConfig.from_env()
    local = datetime.fromtimestamp(now, ZONE)
    if _inside(local.hour, config.sleep_start, config.wake_hour):
        return "night_awake" if 0 <= now - last_human_at < 20 * 60 else "sleeping"
    if (local.hour - config.wake_hour) % 24 < config.wake_window_hours:
        return "waking"
    if (config.sleep_start - local.hour) % 24 <= config.wind_down_hours:
        return "winding_down"
    return "active"


def _energy_delta(phase: str, seconds: float) -> tuple[float, float]:
    hours = seconds / 3600
    rates = {
        "sleeping": (0.11, -0.13),
        "night_awake": (-0.07, 0.09),
        "waking": (0.035, -0.025),
        "active": (-0.026, 0.035),
        "winding_down": (-0.045, 0.06),
    }
    energy, pressure = rates[phase]
    return energy * hours, pressure * hours


class LifeLedger:
    """One global life clock with scope-aware goals and an idempotent event ledger."""

    def __init__(self, repo, clock, config: LifeConfig | None = None):
        self.repo, self.clock = repo, clock
        self.config = config or LifeConfig.from_env()
        self._goal_cache: Dict[str, tuple[tuple[int, int, float, int], list[Dict[str, Any]]]] = {}
        with repo.transaction() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
        self.bootstrap()

    def _earliest_recorded_at(self, fallback: float) -> float:
        values = []
        for table, expression in (
            ("messages", "ts"),
            ("memories", "COALESCE(start_ts,end_ts,created_at)"),
            ("conversations", "created_at"),
            ("experiences", "recorded_at"),
        ):
            try:
                row = self.repo.conn.execute(
                    f"SELECT MIN({expression}) AS value FROM {table} WHERE {expression} > 0"
                ).fetchone()
            except Exception:
                continue
            if row and row["value"] is not None:
                values.append(float(row["value"]))
        return min(values) if values else fallback

    def _load(self, now: float) -> Dict[str, Any]:
        row = self.repo.conn.execute("SELECT * FROM life_state WHERE id='bines'").fetchone()
        if row:
            state = json.loads(row["data"])
            state["_version"] = int(row["version"])
            return state
        born = min(now, self._earliest_recorded_at(now))
        phase = _phase(now, config=self.config)
        return {
            "born_at": born,
            "last_tick": now,
            "phase": phase,
            "energy": 0.9 if phase == "sleeping" else 0.72,
            "sleep_pressure": 0.12 if phase == "sleeping" else 0.28,
            "last_human_at": 0,
            "last_action_at": 0,
            "last_sleep_at": now if phase == "sleeping" else 0,
            "last_wake_at": now if phase != "sleeping" else 0,
            "wake_cycles": 0 if phase == "sleeping" else 1,
            "transitions": [{"phase": phase, "at": now}],
            "_version": 0,
        }

    def _save(self, state: Dict[str, Any], now: float) -> None:
        version = int(state.pop("_version", 0)) + 1
        self.repo.conn.execute(
            "INSERT INTO life_state(id,data,version,updated_at) VALUES('bines',?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data,version=excluded.version,updated_at=excluded.updated_at",
            (_json(state), version, now),
        )
        state["_version"] = version

    def _last_human_at(self, now: float) -> float:
        row = self.repo.conn.execute(
            "SELECT MAX(ts) AS ts FROM life_events WHERE kind='message' AND ts<=? "
            "AND json_extract(payload,'$.role')=? AND json_extract(payload,'$.origin')=?",
            (now, ROLE_USER, ORIGIN_LIVE),
        ).fetchone()
        return float(row["ts"] or 0)

    def _last_action_at(self, now: float) -> float:
        row = self.repo.conn.execute(
            "SELECT MAX(ts) AS ts FROM life_events WHERE kind='experience' AND ts<=? "
            "AND COALESCE(json_extract(payload,'$.record.status'),'') "
            "NOT IN ('','started','empty','retracted')",
            (now,),
        ).fetchone()
        return float(row["ts"] or 0)

    def _advance(self, now: float, *, force: bool = False) -> Dict[str, Any]:
        state = self._load(now)
        old_born_at = float(state.get("born_at") or now)
        born_at = min(now, old_born_at, self._earliest_recorded_at(now))
        state["born_at"] = born_at
        last_tick = min(now, max(born_at, float(state.get("last_tick") or born_at)))
        # Very old installations start from a neutral recent window. Life age is
        # still exact, while unknowable offline physiology is not fabricated.
        cursor = max(last_tick, now - 30 * 86400)
        energy = _number(state.get("energy"), 0.72)
        pressure = _number(state.get("sleep_pressure"), 0.28)
        last_human = self._last_human_at(now)
        last_action = self._last_action_at(now)
        while cursor < now:
            end = min(now, cursor + 3600)
            phase = _phase((cursor + end) / 2, last_human, self.config)
            energy_delta, pressure_delta = _energy_delta(phase, end - cursor)
            energy = _number(energy + energy_delta, energy)
            pressure = _number(pressure + pressure_delta, pressure)
            cursor = end
        previous = state.get("phase") or _phase(last_tick, last_human, self.config)
        current = _phase(now, last_human, self.config)
        if current != previous:
            transitions = list(state.get("transitions") or [])
            transitions.append({"phase": current, "at": now})
            state["transitions"] = transitions[-24:]
            if previous == "sleeping" and current != "sleeping":
                state["last_wake_at"] = now
                state["wake_cycles"] = int(state.get("wake_cycles") or 0) + 1
            elif current == "sleeping":
                state["last_sleep_at"] = now
        state.update(last_tick=now, phase=current, energy=energy,
                     sleep_pressure=pressure, last_human_at=last_human,
                     last_action_at=last_action)
        if force or current != previous or born_at != old_born_at or now - last_tick >= 300:
            self._save(state, now)
        return state

    def _put_event(self, source_ref: str, scope: str, kind: str, ts: float,
                   payload: Dict[str, Any], revision: int = 1) -> bool:
        if not source_ref or not math.isfinite(float(ts)):
            raise ValueError("life event requires a stable source and finite time")
        old = self.repo.conn.execute(
            "SELECT revision FROM life_events WHERE source_ref=?", (source_ref,)
        ).fetchone()
        if old and int(old["revision"]) >= int(revision):
            return False
        self.repo.conn.execute(
            """INSERT INTO life_events(source_ref,scope,kind,ts,payload,revision,recorded_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(source_ref) DO UPDATE SET scope=excluded.scope,kind=excluded.kind,
               ts=excluded.ts,payload=excluded.payload,revision=excluded.revision,recorded_at=excluded.recorded_at""",
            (source_ref, scope, kind, float(ts), _json(payload), int(revision), self.clock()),
        )
        return True

    def record_message(self, message_id: int) -> None:
        row = self.repo.get_message(message_id)
        if not row:
            return
        source_ref = "life:" + (row.get("source_ref") or f"message:{message_id}")
        self._put_event(source_ref, row["scope"], "message", row["ts"], {
            "message_id": message_id, "role": row["role"], "origin": row["origin"],
            "actor_id": row["actor_id"], "conversation_id": row["conversation_id"],
        }, 2 if row["origin"] != ORIGIN_LIVE else 1)
        now = self.clock()
        self._advance(now, force=row["role"] == ROLE_USER)

    def record_experience(self, scope: str, source_ref: str, record: Dict[str, Any], revision: int) -> None:
        ts = float(record.get("finished_at") or record.get("started_at") or self.clock())
        if not self._put_event("life:" + source_ref, scope, "experience", ts,
                               {"record": record}, revision):
            return
        now = self.clock()
        state = self._advance(now)
        if record.get("status") not in {"started", "empty"}:
            state["energy"] = _number(float(state.get("energy") or 0.72) - 0.02, 0.7)
            state["sleep_pressure"] = _number(float(state.get("sleep_pressure") or 0.28) + 0.015, 0.3)
            self._save(state, now)

    def retract_experience(self, source_ref: str) -> None:
        row = self.repo.conn.execute(
            "SELECT revision,scope,ts FROM life_events WHERE source_ref=?", ("life:" + source_ref,)
        ).fetchone()
        if not row:
            return
        self._put_event("life:" + source_ref, row["scope"], "experience", row["ts"],
                        {"record": {"status": "retracted"}}, int(row["revision"]) + 1)

    def bootstrap(self) -> None:
        with self.repo.transaction():
            existing = self.repo.conn.execute("SELECT COUNT(*) AS n FROM life_events").fetchone()["n"]
            if not existing:
                for row in self.repo.conn.execute("SELECT * FROM messages ORDER BY id"):
                    self._put_event("life:" + (row["source_ref"] or f"message:{row['id']}"),
                                    row["scope"], "message", row["ts"], {
                                        "message_id": row["id"], "role": row["role"], "origin": row["origin"],
                                        "actor_id": row["actor_id"], "conversation_id": row["conversation_id"],
                                    }, 2 if row["origin"] != ORIGIN_LIVE else 1)
                try:
                    rows = self.repo.conn.execute("SELECT * FROM experiences ORDER BY recorded_at")
                    for row in rows:
                        record = json.loads(row["record"])
                        ts = float(record.get("finished_at") or record.get("started_at") or row["recorded_at"])
                        self._put_event("life:" + row["source_ref"], row["scope"], "experience", ts,
                                        {"record": record}, row["revision"])
                except Exception:
                    pass
            self._advance(self.clock(), force=True)

    @staticmethod
    def _day_bounds(now: float) -> tuple[float, float]:
        local = datetime.fromtimestamp(now, ZONE)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    def _period_summary(self, start: float, end: float, scope: str | None = None) -> Dict[str, Any]:
        result = {"human_turns": 0, "assistant_turns": 0, "autonomous_actions": 0,
                  "successful_actions": 0, "autonomous_topics": []}
        sql = "SELECT kind,payload FROM life_events WHERE ts>=? AND ts<?"
        params: list[Any] = [start, end]
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        for row in self.repo.conn.execute(sql + " ORDER BY ts", params):
            payload = json.loads(row["payload"])
            if row["kind"] == "message":
                if payload.get("role") == ROLE_USER and payload.get("origin") == ORIGIN_LIVE:
                    result["human_turns"] += 1
                elif payload.get("role") == ROLE_ASSISTANT and payload.get("origin") == ORIGIN_LIVE:
                    result["assistant_turns"] += 1
            else:
                record = payload.get("record") or {}
                if record.get("autonomous") and record.get("category") != "feedback":
                    result["autonomous_actions"] += 1
                    if record.get("status") in {"succeeded", "completed", "sent"}:
                        result["successful_actions"] += 1
                    topic = (record.get("candidate") or {}).get("topic")
                    if topic and topic not in result["autonomous_topics"]:
                        result["autonomous_topics"].append(str(topic)[:120])
        result["autonomous_topics"] = result["autonomous_topics"][:8]
        return result

    def _today(self, now: float, scope: str) -> Dict[str, Any]:
        return self._period_summary(*self._day_bounds(now), scope)

    def day_summary(self, day: str, scope: str = OWNER_SCOPE) -> Dict[str, Any]:
        try:
            start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZONE)
        except (TypeError, ValueError):
            return {}
        result = self._period_summary(start.timestamp(), (start + timedelta(days=1)).timestamp(), scope)
        result["day"] = day
        return result

    def _goals(self, viewer_scope: str, now: float) -> list[Dict[str, Any]]:
        signature_row = self.repo.conn.execute(
            "SELECT COUNT(*) AS n,COALESCE(SUM(revision),0) AS revisions,MAX(recorded_at) AS latest FROM life_events "
            "WHERE kind='experience' AND scope=? AND ts<=?", (viewer_scope, now),
        ).fetchone()
        signature = (int(signature_row["n"]), int(signature_row["revisions"]),
                     float(signature_row["latest"] or 0), int(now // 3600))
        cached = self._goal_cache.get(viewer_scope)
        if cached and cached[0] == signature:
            return [dict(goal) for goal in cached[1]]
        grouped: Dict[str, Dict[str, Any]] = {}
        rows = self.repo.conn.execute(
            "SELECT le.* FROM life_events le LEFT JOIN experiences e "
            "ON le.source_ref='life:'||e.source_ref LEFT JOIN memories m ON m.id=e.memory_id "
            "WHERE le.kind='experience' AND le.scope=? AND le.ts<=? "
            "AND (e.source_ref IS NULL OR m.deleted=0) ORDER BY le.ts,le.source_ref",
            (viewer_scope, now),
        )
        for row in rows:
            record = (json.loads(row["payload"]).get("record") or {})
            candidate = record.get("candidate") or {}
            if (not record.get("autonomous") or not candidate.get("topic")
                    or record.get("category") == "feedback"
                    or record.get("action") == "autonomous_feedback"):
                continue
            turn_id = str(record.get("turn_id") or row["source_ref"])
            goal = grouped.setdefault(turn_id, {
                "id": hashlib.sha256(turn_id.encode()).hexdigest()[:16],
                "title": str(candidate["topic"])[:120],
                "intent": str(candidate.get("intent") or "")[:500],
                "kind": str(candidate.get("kind") or ""),
                "status": "active",
                "created_at": float(record.get("started_at") or row["ts"]),
                "updated_at": float(row["ts"]),
                "source_ref": row["source_ref"].removeprefix("life:"),
                "result": "",
                "_successful": False,
                "_failed": False,
            })
            goal["updated_at"] = max(goal["updated_at"], float(row["ts"]))
            action, status = record.get("action"), record.get("status")
            if action == "autonomous_turn" or record.get("category") == "turn":
                goal["status"] = _TERMINAL.get(status, "active")
                if record.get("result"):
                    goal["result"] = str(record["result"])[:600]
            elif goal["status"] == "active" and status in {"failed", "uncertain", "interrupted"}:
                goal["_failed"] = True
            if status in {"succeeded", "completed", "sent"}:
                goal["_successful"] = True
        goals = []
        for goal in grouped.values():
            if goal["status"] == "active" and now - goal["updated_at"] >= 6 * 3600:
                goal["status"] = "completed" if goal["_successful"] else "interrupted" if goal["_failed"] else "skipped"
            goal.pop("_successful", None)
            goal.pop("_failed", None)
            goals.append(goal)
        goals = sorted(goals, key=lambda item: item["updated_at"], reverse=True)[:20]
        self._goal_cache[viewer_scope] = (signature, goals)
        return [dict(goal) for goal in goals]

    def _milestones(self, state: Dict[str, Any], now: float) -> list[Dict[str, Any]]:
        born = float(state["born_at"])
        milestones = [{"kind": "birth", "title": "Bines 的连续生命记录开始", "at": born}]
        first_message = self.repo.conn.execute(
            "SELECT MIN(ts) AS ts FROM life_events WHERE kind='message' AND ts<=?", (now,)
        ).fetchone()["ts"]
        if first_message is not None:
            milestones.append({"kind": "first_conversation", "title": "第一次有据可查的交流",
                               "at": float(first_message)})
        first_action = self.repo.conn.execute(
            "SELECT MIN(ts) AS ts FROM life_events WHERE kind='experience' "
            "AND json_extract(payload,'$.record.autonomous')=1 AND ts<=?", (now,)
        ).fetchone()["ts"]
        if first_action is not None:
            milestones.append({"kind": "first_autonomy", "title": "第一次有据可查的自主行动",
                               "at": float(first_action)})
        age_days = (datetime.fromtimestamp(now, ZONE).date()
                    - datetime.fromtimestamp(born, ZONE).date()).days + 1
        for days in (7, 30, 100, 365, 1000):
            if age_days >= days:
                milestones.append({"kind": "continuity", "title": f"生命记录连续进入第 {days} 天",
                                   "at": (datetime.fromtimestamp(born, ZONE) + timedelta(days=days - 1)).timestamp()})
        return sorted(milestones, key=lambda item: item["at"])

    def snapshot(self, viewer_scope: str = OWNER_SCOPE, now: float | None = None) -> Dict[str, Any]:
        now = self.clock() if now is None else float(now)
        with self.repo.transaction():
            state = self._advance(now)
            born = float(state["born_at"])
            local_now = datetime.fromtimestamp(now, ZONE)
            age_days = (local_now.date() - datetime.fromtimestamp(born, ZONE).date()).days + 1
            goals = self._goals(viewer_scope, now)
            result = {
                "born_at": born,
                "age_seconds": max(0, now - born),
                "age_days": max(1, age_days),
                "day_number": max(1, age_days),
                "phase": state["phase"],
                "phase_label": _PHASE_LABELS[state["phase"]],
                "energy": round(_number(state.get("energy"), 0.72), 4),
                "sleep_pressure": round(_number(state.get("sleep_pressure"), 0.28), 4),
                "last_human_at": float(state.get("last_human_at") or 0),
                "last_action_at": float(state.get("last_action_at") or 0),
                "last_sleep_at": float(state.get("last_sleep_at") or 0),
                "last_wake_at": float(state.get("last_wake_at") or 0),
                "wake_cycles": int(state.get("wake_cycles") or 0),
                "today": self._today(now, viewer_scope),
                "current_focus": next((goal["title"] for goal in goals if goal["status"] == "active"), ""),
            }
            if viewer_scope == OWNER_SCOPE:
                result.update(goals=goals, milestones=self._milestones(state, now),
                              transitions=list(state.get("transitions") or []))
            return result

    def event_count(self) -> int:
        return int(self.repo.conn.execute("SELECT COUNT(*) AS n FROM life_events").fetchone()["n"])
