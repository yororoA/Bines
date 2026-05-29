from __future__ import annotations

from datetime import datetime, timedelta


def day_key(timestamp: datetime | None = None) -> str:
    from thinking_settings import thinking_settings
    dt = timestamp or datetime.now()
    if dt.hour < thinking_settings.DAY_KEY_CUTOFF_HOUR:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


STREAM_TOKEN_LIMIT = 8192
SUMMARY_TOKEN_WINDOW = 4096