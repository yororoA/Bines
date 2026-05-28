from __future__ import annotations

from datetime import datetime, timedelta


def day_key(timestamp: datetime | None = None) -> str:
    dt = timestamp or datetime.now()
    if dt.hour < 4:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


STREAM_TOKEN_LIMIT = 8192
SUMMARY_TOKEN_WINDOW = 4096