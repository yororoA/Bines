"""Per-request accounting without retaining prompts, answers, or credentials."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_SINK = ContextVar("model_usage_sink", default=None)
log = logging.getLogger("bines.model_usage")


@contextmanager
def capture_model_usage(sink):
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def _value(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def measured_request(create, **kwargs):
    sink = _SINK.get()
    if sink is None:
        return create(**kwargs)
    event = {"call_id": uuid.uuid4().hex, "model": str(kwargs.get("model", "")),
             "status": "started", "started_at": time.time()}
    # Persist before spending tokens. An interrupted process leaves a visible
    # unknown-cost request rather than silently claiming zero usage.
    sink(dict(event))
    started = time.monotonic()
    try:
        response = create(**kwargs)
    except Exception as exc:
        event.update(status="error", error_type=type(exc).__name__)
        raise
    else:
        choices = getattr(response, "choices", None) or []
        reason = getattr(choices[0], "finish_reason", None) if choices else None
        event.update(status="truncated" if reason == "length" else "returned",
                     finish_reason=reason)
        usage = getattr(response, "usage", None)
        event["usage"] = {key: _value(usage, key) for key in
                          ("prompt_tokens", "completion_tokens", "total_tokens",
                           "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")}
        event["usage"]["reasoning_tokens"] = _value(_value(usage, "completion_tokens_details"), "reasoning_tokens")
        event["usage"]["cached_tokens"] = _value(_value(usage, "prompt_tokens_details"), "cached_tokens")
        return response
    finally:
        event["elapsed_seconds"] = round(time.monotonic() - started, 6)
        try:
            sink(event)
        except Exception:
            # A start record remains. Losing billing telemetry must not replay a
            # completed generation or discard its result.
            log.warning("model accounting completion unavailable; request remains unknown: %s", event["call_id"])
