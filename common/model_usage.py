"""Per-request accounting without retaining prompts, answers, or credentials."""
from __future__ import annotations

import logging
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_SINK = ContextVar("model_usage_sink", default=None)
_KIND = ContextVar("model_usage_kind", default="")
log = logging.getLogger("bines.model_usage")


class BackgroundBudgetDeferred(RuntimeError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__("background daily budget deferred")


@contextmanager
def capture_model_usage(sink, *, kind=""):
    token = _SINK.set(sink)
    kind_token = _KIND.set(kind or _KIND.get())
    try:
        yield
    finally:
        _SINK.reset(token)
        _KIND.reset(kind_token)


@contextmanager
def model_usage_kind(kind):
    token = _KIND.set(kind)
    try:
        yield
    finally:
        _KIND.reset(token)


def usage_path():
    root = Path(os.environ.get("BINES_MEMORY_DIR") or
                Path(__file__).resolve().parents[1] / "thingking" / "memory_data")
    return Path(os.environ.get("BINES_MODEL_USAGE_PATH") or root / "model_usage.db")


def record_usage(event):
    """One cross-process ledger, separate from autobiographical memory.

    Only counts and task categories are persisted. The worker's job ledger is
    a reservation/audit mirror with the same call_id, not additional usage.
    """
    path = usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS model_calls (
            call_id TEXT PRIMARY KEY, kind TEXT NOT NULL, model TEXT NOT NULL,
            started_at REAL NOT NULL, status TEXT NOT NULL, elapsed_seconds REAL,
            usage TEXT, estimated_input_units INTEGER, counting_method TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS model_calls_time ON model_calls(started_at,kind)")
        db.execute("""INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(call_id) DO UPDATE SET status=excluded.status,
            elapsed_seconds=excluded.elapsed_seconds,usage=excluded.usage
            WHERE model_calls.status='started' AND excluded.status!='started'""",
            (event["call_id"], event.get("kind", "other"), event.get("model", ""),
             event["started_at"], event["status"], event.get("elapsed_seconds"),
             json.dumps(event.get("usage"), separators=(",", ":")),
             event.get("estimated_input_units"), event.get("counting_method")))


def usage_report(path=None, *, hours=24):
    """Read aggregate provider usage only; never mix the worker audit mirror."""
    path = Path(path or usage_path())
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""WITH calls AS (
            SELECT *,json_extract(usage,'$.prompt_tokens') AS input,
                json_extract(usage,'$.completion_tokens') AS output,
                COALESCE(json_extract(usage,'$.prompt_cache_hit_tokens'),
                         json_extract(usage,'$.cached_tokens')) AS hit
            FROM model_calls WHERE started_at>=?)
            SELECT kind,model,COUNT(*) AS calls,
                SUM(input IS NULL OR output IS NULL) AS unknown_usage_calls,
                COALESCE(SUM(input),0) AS prompt_tokens,COALESCE(SUM(output),0) AS completion_tokens,
                SUM(hit IS NULL) AS unknown_cache_calls,COALESCE(SUM(hit),0) AS cached_tokens,
                SUM(CASE WHEN hit IS NOT NULL THEN input END) AS cache_measured_prompt_tokens,
                SUM(status='error') AS errors,SUM(status='truncated') AS truncated
            FROM calls GROUP BY kind,model ORDER BY kind,model""",
            (time.time() - max(0, float(hours)) * 3600,)).fetchall()
    return [{**dict(row), "cache_hit_ratio": row["cached_tokens"] / row["cache_measured_prompt_tokens"]
             if row["cache_measured_prompt_tokens"] else None} for row in rows]


def _kind():
    if _KIND.get():
        return _KIND.get()
    from tools.context import CURRENT_CONTEXT
    context = CURRENT_CONTEXT.get()
    if context.autonomous:
        return "autonomous"
    return "qq" if context.event_id else "desktop"


def _usage(response):
    usage = _value(response, "usage")
    if usage is None:
        return None
    result = {key: _value(usage, key) for key in
              ("prompt_tokens", "completion_tokens", "total_tokens",
               "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")}
    result["reasoning_tokens"] = _value(_value(usage, "completion_tokens_details"), "reasoning_tokens")
    result["cached_tokens"] = _value(_value(usage, "prompt_tokens_details"), "cached_tokens")
    return {key: value if type(value) is int and value >= 0 else None for key, value in result.items()}


def _returned(event, response):
    choices = _value(response, "choices") or []
    reason = _value(choices[0], "finish_reason") if choices else None
    if reason:
        event["finish_reason"] = reason
    event["status"] = "truncated" if event.get("finish_reason") == "length" else "returned"
    usage = _usage(response)
    if usage is not None:
        event["usage"] = usage


class _MeasuredStream:
    def __init__(self, stream, event, finish):
        self.stream, self.event, self.finish = stream, event, finish
        self.iterator = iter(stream)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self.iterator)
        except StopIteration:
            self.close()
            raise
        except BaseException as exc:
            self.event.update(status="error", error_type=type(exc).__name__)
            self.close()
            raise
        _returned(self.event, chunk)
        return chunk

    def close(self):
        if self.closed:
            return
        self.closed = True
        # A stream closed before its usage chunk has unknown billing even if it
        # yielded text. Do not invent zeros or retry the generation.
        if not self.event.get("finish_reason") and self.event["status"] != "error":
            self.event["status"] = "interrupted"
        try:
            close = getattr(self.stream, "close", None)
            if callable(close):
                close()
        finally:
            self.finish()


def _value(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def measured_request(create, **kwargs):
    sink = _SINK.get()
    from common.context_budget import ContextTokenCounter
    counter = ContextTokenCounter()
    event = {"call_id": uuid.uuid4().hex, "model": str(kwargs.get("model", "")),
             "kind": _kind(), "status": "started", "started_at": time.time(),
             "estimated_input_units": counter.messages(kwargs.get("messages") or [])
                                      + counter.count(kwargs.get("tools") or []),
             "counting_method": counter.method,
             "max_output_tokens": kwargs.get("max_tokens", 0)}
    # Persist before spending tokens. An interrupted process leaves a visible
    # unknown-cost request rather than silently claiming zero usage.
    if sink:
        sink(dict(event))
    def persist():
        try:
            record_usage(dict(event))
        except Exception:
            log.warning("model usage ledger unavailable: %s", event["call_id"])
    persist()
    started = time.monotonic()
    def finish():
        event["elapsed_seconds"] = round(time.monotonic() - started, 6)
        persist()
        if sink:
            try:
                sink(dict(event))
            except Exception:
                log.warning("model accounting completion unavailable: %s", event["call_id"])
    if kwargs.get("stream"):
        kwargs["stream_options"] = {**(kwargs.get("stream_options") or {}), "include_usage": True}
    try:
        response = create(**kwargs)
    except BaseException as exc:
        event.update(status="error", error_type=type(exc).__name__)
        finish()
        raise
    if kwargs.get("stream"):
        return _MeasuredStream(response, event, finish)
    _returned(event, response)
    finish()
    return response


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="模型用量与缓存命中统计；未知用量不计为零")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--path")
    args = parser.parse_args()
    print("分类 · 模型 | 请求 | 输入 | 输出 | 缓存命中 | 未知用量")
    for row in usage_report(args.path, hours=args.hours):
        ratio = "未知" if row["cache_hit_ratio"] is None else f'{row["cache_hit_ratio"]:.1%}'
        print(f'{row["kind"]} · {row["model"]} | {row["calls"]} | {row["prompt_tokens"]} | '
              f'{row["completion_tokens"]} | {ratio} | {row["unknown_usage_calls"]}')
