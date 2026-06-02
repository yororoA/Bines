from __future__ import annotations

from datetime import datetime, timedelta


def day_key(timestamp: datetime | None = None) -> str:
    from thinking_settings import thinking_settings
    dt = timestamp or datetime.now()
    if dt.hour < thinking_settings.DAY_KEY_CUTOFF_HOUR:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


_CJK_RANGES = [
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x2E80, 0x2EFF),
    (0x3000, 0x303F),
    (0xFF00, 0xFFEF),
]


def estimate_tokens(text: str) -> int:
    tokens = 0.0
    for ch in text:
        code = ord(ch)
        if any(start <= code <= end for start, end in _CJK_RANGES):
            tokens += 1.5
        else:
            tokens += 0.25
    return int(tokens)


DIARY_TOKEN_LIMIT = 10240