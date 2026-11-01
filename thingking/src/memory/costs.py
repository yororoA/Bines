"""Idempotent model-call ledger. Missing API usage is unknown, never zero."""
import math
import os
from datetime import timedelta


def budget_lane(kind):
    return "association" if kind == "associate" else "state" if kind == "update_state" else "maintenance"


class ModelCostStore:
    def __init__(self, repo):
        self.repo = repo
        repo.conn.execute("""CREATE TABLE IF NOT EXISTS memory_model_calls (
            call_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
            kind TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
            started_at REAL NOT NULL, elapsed_seconds REAL, prompt_tokens INTEGER,
            completion_tokens INTEGER, total_tokens INTEGER, reasoning_tokens INTEGER,
            cached_tokens INTEGER, cache_hit_tokens INTEGER, cache_miss_tokens INTEGER,
            finish_reason TEXT, error_type TEXT)""")
        repo.conn.execute("CREATE INDEX IF NOT EXISTS model_calls_job ON memory_model_calls(job_id,attempt)")
        repo.conn.execute("""CREATE TABLE IF NOT EXISTS memory_budget_reservations (
            call_id TEXT PRIMARY KEY, lane TEXT NOT NULL, started_at REAL NOT NULL,
            reserved_tokens INTEGER NOT NULL, actual_tokens INTEGER)""")
        repo.conn.execute("CREATE INDEX IF NOT EXISTS budget_time ON memory_budget_reservations(lane,started_at)")
        # Include pre-upgrade calls in today's budget; unknown old usage stays
        # conservatively charged instead of granting a fresh budget at restart.
        repo.conn.execute("""INSERT OR IGNORE INTO memory_budget_reservations
            SELECT call_id,CASE kind WHEN 'associate' THEN 'association' WHEN 'update_state' THEN 'state'
                ELSE 'maintenance' END,started_at,32768,
                CASE WHEN prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL
                    THEN prompt_tokens+completion_tokens END FROM memory_model_calls""")

    def reserve(self, job, event):
        from common.clock import now
        current = now(self.repo.clock())
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=1)).timestamp()
        lane = budget_lane(job["kind"])
        calls, tokens = {"association": (100, 300000), "maintenance": (300, 1500000),
                         "state": (1000, 2000000)}[lane]
        calls = max(1, int(os.environ.get(f"BINES_MEMORY_{lane.upper()}_DAILY_CALLS", calls)))
        tokens = max(1, int(os.environ.get(f"BINES_MEMORY_{lane.upper()}_DAILY_TOKENS", tokens)))
        # UTF-8 upper bounds are intentionally labelled estimates. Usage from
        # the API reconciles them; missing usage keeps its reservation all day.
        estimate = max(1, int(event.get("estimated_input_units") or 32768))
        estimate += max(0, int(event.get("max_output_tokens") or 0))
        row = self.repo.conn.execute("""SELECT COUNT(*),COALESCE(SUM(COALESCE(actual_tokens,reserved_tokens)),0)
            FROM memory_budget_reservations WHERE lane=? AND started_at>=? AND started_at<?""",
            (lane, start.timestamp(), end)).fetchone()
        if row[0] >= calls or row[1] + estimate > tokens:
            return {"status": "deferred", "retry_at": end, "lane": lane}
        self.repo.conn.execute("INSERT INTO memory_budget_reservations VALUES(?,?,?,?,NULL)",
                               (event["call_id"], lane, self.repo.clock(), estimate))
        return None

    def record(self, job, attempt, event, max_calls=None, *, daily_limits=False):
        call_id = event.get("call_id")
        status = event.get("status")
        if (not isinstance(call_id, str) or len(call_id) != 32
                or any(c not in "0123456789abcdef" for c in call_id)
                or status not in {"started", "error", "truncated", "returned"}):
            raise ValueError("模型调用记录无效")
        usage = event.get("usage") or {}
        def number(key):
            value = usage.get(key)
            return value if type(value) is int and 0 <= value <= 100_000_000 else None
        started_at = event.get("started_at")
        elapsed = event.get("elapsed_seconds")
        if not isinstance(started_at, (int, float)) or not math.isfinite(started_at):
            raise ValueError("模型调用时间无效")
        if elapsed is not None and (not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError("模型调用耗时无效")
        existing = self.repo.conn.execute("SELECT * FROM memory_model_calls WHERE call_id=?", (call_id,)).fetchone()
        if existing and (existing["job_id"] != job["id"] or existing["attempt"] != attempt):
            raise ValueError("模型调用编号不可跨任务使用")
        if existing and (existing["status"] != "started" or status == "started"):
            return {"status": "duplicate"}
        if status == "started" and max_calls is not None:
            count = self.repo.conn.execute("SELECT COUNT(*) FROM memory_model_calls WHERE job_id=?", (job["id"],)).fetchone()[0]
            if count >= max_calls:
                return {"status": "budget_exhausted", "calls": count, "limit": max_calls}
        if status == "started" and daily_limits:
            deferred = self.reserve(job, event)
            if deferred:
                return deferred
        values = (call_id, job["id"], attempt, job["kind"], str(event.get("model", ""))[:160],
                  status, started_at, elapsed, number("prompt_tokens"), number("completion_tokens"),
                  number("total_tokens"), number("reasoning_tokens"), number("cached_tokens"),
                  number("prompt_cache_hit_tokens"), number("prompt_cache_miss_tokens"),
                  str(event.get("finish_reason") or "")[:80], str(event.get("error_type") or "")[:80])
        self.repo.conn.execute("INSERT OR REPLACE INTO memory_model_calls VALUES(" + ",".join("?" * 17) + ")", values)
        if number("prompt_tokens") is not None and number("completion_tokens") is not None:
            self.repo.conn.execute("UPDATE memory_budget_reservations SET actual_tokens=? WHERE call_id=?",
                (number("prompt_tokens") + number("completion_tokens"), call_id))
        return {"status": "recorded"}

    def stats(self):
        return [dict(row) for row in self.repo.conn.execute("""SELECT kind,COUNT(*) calls,
            SUM(CASE WHEN prompt_tokens IS NULL OR completion_tokens IS NULL THEN 1 ELSE 0 END) unknown_usage_calls,
            COALESCE(SUM(prompt_tokens),0) prompt_tokens,COALESCE(SUM(completion_tokens),0) completion_tokens,
            COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,COALESCE(SUM(elapsed_seconds),0) elapsed_seconds,
            SUM(CASE WHEN status='truncated' THEN 1 ELSE 0 END) truncated_calls,
            SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) error_calls
            FROM memory_model_calls GROUP BY kind ORDER BY kind""")]
