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


def summary_projection(record):
    """Select autobiographical evidence; retain the full receipt in experiences.

    An allowlist at runtime-envelope boundaries prevents diagnostics, recalled
    citation inventories and old candidate snapshots from becoming new memories.
    Tool result bodies remain evidence, including unknown/new tool fields.
    """
    def excerpt(value, limit):
        raw = text(value)
        if len(raw) <= limit:
            return value
        return raw[:limit * 2 // 3] + "\n[节选；完整内容见原始回执]\n" + raw[-limit // 3:]

    projected = {key: record[key] for key in (
        "category", "action", "skill", "status", "autonomous", "started_at", "finished_at",
        "duration_seconds", "turn_id", "source_message_refs", "fact_corrections",
        "memory_corrections", "correction_results",
    ) if key in record}
    projected["args"] = excerpt(record.get("args") or {}, 1800)
    projected["result"] = excerpt(record.get("result") or "", 9000)
    candidate = record.get("candidate") or {}
    projected["intent"] = {key: candidate[key] for key in ("topic", "intent", "reason") if key in candidate}
    activity = record.get("activity") or {}
    projected["activity"] = {key: activity[key] for key in (
        "id", "sequence", "mode", "intent", "note", "topic", "reason", "decision_source", "decision",
    ) if key in activity}
    # Observations have their own raw receipts. A compact checkpoint still keeps
    # useful observations when older producers did not journal individual tools.
    if activity.get("observations"):
        projected["activity"]["observations"] = [
            {key: excerpt(item[key], 1600) for key in ("action", "status", "result") if key in item}
            for item in activity["observations"][-4:] if isinstance(item, dict)
        ]
    return projected
