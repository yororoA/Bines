"""Bounded daily evidence: conversation, original exchanges and grouped activities.

All source episodes remain in the consolidation signature. Selection here only
changes what is presented to the writer, never the stored evidence or its scope.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import os

from .dependencies import direct_dependencies
from .evidence import _active
from .attribution import speaker

POLICY = "diary-evidence-v3-attribution"


def _bytes(text):
    return len(text.encode("utf-8"))


def _excerpt(text, budget):
    if _bytes(text) <= budget:
        return text
    marker = "…[节选]…"
    remaining = max(0, budget - _bytes(marker))
    if not remaining:
        return ""
    return text.encode("utf-8")[:remaining].decode("utf-8", errors="ignore") + marker


def _fit(rows, budget, *, item_limit=1800):
    """Give each event a share before allowing an unusually long one to dominate."""
    if not rows or budget <= 0:
        return []
    capacity = max(1, budget // 500)
    if len(rows) > capacity:
        # Cover the supplied sequence, including its ends, when a day contains
        # more independent events than the writer can receive in one request.
        rows = [rows[round(i * (len(rows) - 1) / max(1, capacity - 1))] for i in range(capacity)]
    chosen, remaining = [], budget
    for index, row in enumerate(rows):
        share = min(item_limit, remaining // max(1, len(rows) - index))
        if share < 100:
            break
        if row.get("atomic") and _bytes(row["text"]) > share:
            continue  # Never show an old quote without its complete correction.
        body = _excerpt(row["text"], share)
        if body:
            item = {**row, "text": body}
            if "messages" in row:
                item["messages"] = [m for m in row["messages"] if f"[原话#{m['id']} " in body]
            else:
                item["source_ids"] = [sid for sid in row["source_ids"] if sid in body]
            chosen.append(item)
            remaining -= _bytes(body)
    return chosen


def _record(repo, episode):
    ref = episode["meta"].get("experience_ref")
    if not ref:
        return {}
    row = repo.conn.execute("SELECT record FROM experiences WHERE source_ref=? AND scope=?",
                            (ref, episode["scope"])).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return {}


def _stamp(episode):
    return episode.get("end_ts") or episode.get("created_at") or 0


def _message_corrections(repo, message):
    rows = repo.conn.execute("""SELECT DISTINCT c.seq,c.quote,c.replacement,c.explanation,c.operation_id
        FROM memory_corrections c JOIN association_nodes n ON n.id=c.node_id
        JOIN json_each(n.family) f
        WHERE c.scope=? AND n.scope=? AND c.kind='fact' AND f.value=? ORDER BY c.seq""",
        (message["scope"], message["scope"], f"message:{message['id']}"))
    return [dict(row) for row in rows if row["quote"] and row["quote"] in message["content"]]


def _message_fingerprint(message, corrections=()):
    fields = ("id", "scope", "day_key", "conversation_id", "role", "actor_id", "actor_name", "ts", "origin", "content")
    return hashlib.sha256(json.dumps({**{k: message.get(k) for k in fields}, "corrections": list(corrections)},
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _interactions(repo, episodes, scope, day):
    seen, exchanges = set(), []
    for episode in episodes:
        ids = [int(value) for kind, value in direct_dependencies(repo, episode) if kind == "message"]
        messages = []
        for offset in range(0, len(ids), 400):
            batch = ids[offset:offset + 400]
            if batch:
                messages.extend(dict(row) for row in repo.conn.execute(
                    "SELECT * FROM messages WHERE scope=? AND day_key=? AND id IN ("
                    + ",".join("?" for _ in batch) + ") ORDER BY ts,id", (scope, day, *batch)))
        messages = sorted((m for m in messages if _active(m, "message", include_archived=True)),
                          key=lambda m: (m["ts"], m["id"]))
        users = [i for i, m in enumerate(messages) if m["role"] == "user"]
        # Spread original exchanges across the day instead of taking only its
        # last few turns. Summaries still describe the other conversations.
        for position in dict.fromkeys(users[:1] + users[-1:]):
            user = messages[position]
            if user["id"] in seen:
                continue
            pair = [user]
            for following in messages[position + 1:]:
                if following["conversation_id"] != user["conversation_id"]:
                    continue
                if following["role"] in {"user", "observed"}:
                    break
                if following["role"] == "assistant":
                    pair.append(following)
                    break
            seen.update(m["id"] for m in pair)
            pieces, constraints = [], []
            for message in pair:
                corrections = _message_corrections(repo, message)
                constraints.extend(corrections)
                annotation = ("[已核验纠正，原话中的旧事实已失效：" + json.dumps(
                    [{"quote": c["quote"], "replacement": c["replacement"]} for c in corrections], ensure_ascii=False) + "]\n") if corrections else ""
                pieces.append(annotation + f"[原话#{message['id']} {speaker(message)}] " + _excerpt(message["content"], 900))
            exchanges.append({"text": "\n".join(pieces), "messages": pair, "source_ids": [episode["id"]],
                              "corrections": constraints, "atomic": bool(constraints)})
    return exchanges


def _activities(repo, episodes):
    groups = defaultdict(list)
    for episode in episodes:
        record = _record(repo, episode)
        candidate = record.get("candidate") or {}
        activity = record.get("activity") or candidate.get("activity") or {}
        topic = candidate.get("topic") or activity.get("topic") or ""
        action = episode["meta"].get("action") or record.get("action") or "经历"
        identity = activity.get("id") or topic or (episode["meta"].get("urls") or [action])[0]
        groups[str(identity)].append(episode)
    rows = []
    for entries in groups.values():
        entries.sort(key=lambda e: (_stamp(e), e["id"]))
        statuses = Counter(str(e["meta"].get("status") or "unknown") for e in entries)
        # Keep first/last observations and exceptional outcomes. Failure and
        # uncertain receipts must not be silently rewritten as success.
        exceptional = [e for e in entries if e["meta"].get("status") in
                       {"failed", "uncertain", "interrupted", "skipped"}]
        selected = {e["id"]: e for e in [entries[0], *exceptional[-2:], entries[-1]]}
        selected = sorted(selected.values(), key=lambda e: (_stamp(e), e["id"]))
        body = "\n".join(f"[经历 {e['id']}; 状态={e['meta'].get('status', 'unknown')}] "
                         + _excerpt(e["content"], 1100) for e in selected)
        rows.append({"text": "[同一活动的证据节选；状态覆盖="
                     + ",".join(sorted(statuses)) + "]\n" + body,
                     "source_ids": [e["id"] for e in selected], "all_source_ids": [e["id"] for e in entries],
                     "importance": max(e.get("importance") or 0 for e in entries),
                     "timestamp": _stamp(entries[-1])})
    return sorted(rows, key=lambda row: (-row["importance"], -row["timestamp"], row["source_ids"]))


def select_materials(repo, episodes, scope, day, *, budget=None):
    budget = max(4000, min(96000, int(budget if budget is not None else
                                   os.environ.get("BINES_MEMORY_DIARY_MATERIAL_BYTES", "24000"))))
    ordered = sorted((e for e in episodes if e["scope"] == scope and e.get("day_key") == day
                      and _active(e, "memory")), key=lambda e: (_stamp(e), e["id"]))
    conversations = [e for e in ordered if not e["meta"].get("experience_ref")]
    experiences = [e for e in ordered if e["meta"].get("experience_ref")]
    summaries = [{"text": f"[当日对话摘要 {e['id']}] " + e["content"], "source_ids": [e["id"]]}
                 for e in conversations]
    interactions = _interactions(repo, conversations, scope, day)
    activities = _activities(repo, experiences)
    if summaries or interactions:
        summary_rows = _fit(summaries, budget * 2 // 5, item_limit=3000)
        quote_rows = _fit(interactions, budget * 7 // 20, item_limit=1900)
        activity_rows = _fit(activities, budget // 4, item_limit=3600)
    else:
        summary_rows, quote_rows = [], []
        activity_rows = _fit(activities, budget, item_limit=3600)
    chosen = summary_rows + activity_rows
    text_bytes = sum(_bytes(row["text"]) for row in chosen + quote_rows)
    message_versions = {str(m["id"]): _message_fingerprint(m, _message_corrections(repo, m))
                        for row in quote_rows for m in row["messages"]}
    return {
        "episodes": [row["text"] for row in chosen],
        "interaction_evidence": [row["text"] for row in quote_rows],
        "participants": list(dict.fromkeys(speaker(m) for row in quote_rows for m in row["messages"])),
        "material_selection": {
            "policy": POLICY, "budget_bytes": budget, "selected_bytes": text_bytes,
            "conversation_episodes": len(conversations), "experience_episodes": len(experiences),
            "activity_groups": len(activities), "presented_activity_groups": len(activity_rows),
            "presented_interactions": len(quote_rows),
            "presented_source_ids": list(dict.fromkeys(s for row in chosen + quote_rows for s in row["source_ids"])),
            "message_versions": message_versions,
            "corrections": list({c["operation_id"]: c for row in quote_rows for c in row.get("corrections", [])}.values()),
        },
    }


def selection_is_current(repo, selection, scope, day):
    for source_id, fingerprint in selection.get("message_versions", {}).items():
        message = repo.get_message(int(source_id))
        if (not message or message["scope"] != scope or message["day_key"] != day
                or not _active(message, "message", include_archived=True)
                or _message_fingerprint(message, _message_corrections(repo, message)) != fingerprint):
            return False
    return True


def archive_diary_revision(repo, diary, job_id):
    """Keep the previous diary verbatim when its evidence or voice is updated."""
    repo.conn.execute("""CREATE TABLE IF NOT EXISTS diary_revisions (
        memory_id TEXT NOT NULL, job_id INTEGER NOT NULL, scope TEXT NOT NULL,
        day_key TEXT NOT NULL, content TEXT NOT NULL, meta TEXT NOT NULL,
        archived_at REAL NOT NULL, PRIMARY KEY(memory_id,job_id))""")
    repo.conn.execute("INSERT OR IGNORE INTO diary_revisions VALUES(?,?,?,?,?,?,?)",
                      (diary["id"], job_id, diary["scope"], diary["day_key"], diary["content"],
                       json.dumps(diary["meta"], ensure_ascii=False), repo.clock()))
