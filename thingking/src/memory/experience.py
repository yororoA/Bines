"""Tool experiences are assistant observations, never statements by a person."""
import json
import re

from common.memory_journal import safe_data


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def urls(record):
    return list(dict.fromkeys(re.findall(r"https?://[^\s<>\"'\\]+", text(record))))[:100]


def summary(record):
    action = record.get("action") or "行动"
    status = record.get("status") or "uncertain"
    candidate = record.get("candidate") or {}
    topic = candidate.get("topic") or candidate.get("intent") or ""
    body = text(record.get("result", ""))
    preview = body if len(body) <= 1400 else body[:1100] + "\n…[完整结果见来源证据]…\n" + body[-300:]
    args = text(record.get("args") or {})
    link_text = "\n".join(urls(record)[:12])
    return (
        f"Bines{'自主' if record.get('autonomous') else ''}经历：{topic} {action}；状态={status}。\n"
        f"操作参数：{args[:900]}\n结果或反思（工具观察，不是用户原话）：{preview}"
        + (f"\n来源链接：\n{link_text}" if link_text else "")
    )


def normalize(record):
    if not isinstance(record, dict):
        raise ValueError("record 必须是对象")
    result = safe_data(record)
    if result.get("status") not in {"started", "succeeded", "failed", "uncertain", "skipped",
                                    "empty", "completed", "sent", "interrupted"}:
        raise ValueError("未知的经历状态")
    return result
