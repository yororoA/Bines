"""Idempotent model-call ledger. Missing API usage is unknown, never zero."""
import math


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

    def record(self, job, attempt, event, max_calls=None):
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
        values = (call_id, job["id"], attempt, job["kind"], str(event.get("model", ""))[:160],
                  status, started_at, elapsed, number("prompt_tokens"), number("completion_tokens"),
                  number("total_tokens"), number("reasoning_tokens"), number("cached_tokens"),
                  number("prompt_cache_hit_tokens"), number("prompt_cache_miss_tokens"),
                  str(event.get("finish_reason") or "")[:80], str(event.get("error_type") or "")[:80])
        self.repo.conn.execute("INSERT OR REPLACE INTO memory_model_calls VALUES(" + ",".join("?" * 17) + ")", values)
        return {"status": "recorded"}

    def stats(self):
        return [dict(row) for row in self.repo.conn.execute("""SELECT kind,COUNT(*) calls,
            SUM(CASE WHEN prompt_tokens IS NULL OR completion_tokens IS NULL THEN 1 ELSE 0 END) unknown_usage_calls,
            COALESCE(SUM(prompt_tokens),0) prompt_tokens,COALESCE(SUM(completion_tokens),0) completion_tokens,
            COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,COALESCE(SUM(elapsed_seconds),0) elapsed_seconds,
            SUM(CASE WHEN status='truncated' THEN 1 ELSE 0 END) truncated_calls,
            SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) error_calls
            FROM memory_model_calls GROUP BY kind ORDER BY kind""")]
