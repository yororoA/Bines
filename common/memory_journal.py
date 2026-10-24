"""Durable memory outbox and per-turn journals, independent of model output."""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
import math
from pathlib import Path

log = logging.getLogger("bines.memory.journal")
_SECRET = re.compile(r"^(password|passwd|secret|api_key|access_token|authorization|cookie|set-cookie)$", re.I)


def safe_data(value):
    """Keep textual evidence intact; omit credentials and inline binary payloads."""
    if isinstance(value, dict):
        return {str(key): "[凭据已省略]" if _SECRET.match(str(key)) else safe_data(item)
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [safe_data(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("data:image/", "data:audio/", "base64://")):
            return "[内联媒体数据已省略]"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    return str(value)


def result_data(result):
    if isinstance(result, str):
        for parse in (json.loads, ast.literal_eval):
            try:
                result = parse(result)
                break
            except (ValueError, SyntaxError, TypeError):
                pass
    return safe_data(result)


def outcome_status(result):
    data = result_data(result)
    if isinstance(data, dict):
        status = str(data.get("status", "")).lower()
        if status in {"blocked_human", "paused", "skipped"} or data.get("human_action_required"):
            return "skipped"
        if status in {"uncertain", "started"}:
            return "uncertain"
        if status in {"error", "failed", "failure"} or data.get("retcode", 0) not in (0, "0", None):
            return "failed"
    text = str(result or "")
    if text.startswith(("自主行动未执行", "自主行动已取消")):
        return "skipped"
    if text.startswith(("技能执行失败", "技能协议错误", "Error:", "错误", "无法", "未找到",
                        "状态更新失败", "Dynamic memory is not available", "[图片无法", "[消息含图片")):
        return "failed"
    return "succeeded" if text else "empty"


class MemoryOutbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS memory_outbox (
                source_ref TEXT PRIMARY KEY, method TEXT NOT NULL, params TEXT NOT NULL,
                revision INTEGER NOT NULL, synced INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS memory_outbox_pending ON memory_outbox(synced,updated)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, source_ref, method, params):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT revision FROM memory_outbox WHERE source_ref=?", (source_ref,)).fetchone()
            revision = (previous["revision"] if previous else 0) + 1
            params = {**params, "source_ref": source_ref}
            if method == "record_experience":
                params["revision"] = revision
            db.execute("""INSERT INTO memory_outbox VALUES(?,?,?,?,0,?)
                ON CONFLICT(source_ref) DO UPDATE SET method=excluded.method,params=excluded.params,
                revision=excluded.revision,synced=0,updated=excluded.updated""",
                (source_ref, method, json.dumps(params, ensure_ascii=False, allow_nan=False), revision, time.time()))
        return revision

    def sync(self, call, limit=500):
        with self.lock:
            with self.connection() as db:
                rows = db.execute("SELECT * FROM memory_outbox WHERE synced=0 ORDER BY updated,source_ref LIMIT ?",
                                  (limit,)).fetchall()
            for row in rows:
                call(row["method"], **json.loads(row["params"]))
                with self.connection() as db:
                    db.execute("UPDATE memory_outbox SET synced=1 WHERE source_ref=? AND revision=?",
                               (row["source_ref"], row["revision"]))
            return len(rows)

    def start(self, client):
        if self.thread and self.thread.is_alive():
            return
        def consume():
            while not self.stop.is_set():
                try:
                    count = self.sync(client.call)
                    self.stop.wait(0.1 if count == 500 else 1)
                except Exception as exc:
                    log.warning("记忆待补同步：%s", exc)
                    self.stop.wait(5)
        self.thread = threading.Thread(target=consume, name="MemoryOutbox", daemon=True)
        self.thread.start()

    def record_message(self, envelope, role, content, source_ref, **fields):
        from memory.models import is_system_event
        if not content.strip() or (role == "user" and is_system_event(content)):
            return
        self.put(source_ref, "record_message", {
            "envelope": envelope, "role": role, "content": content, **fields,
        })


class MemoryJournal:
    def __init__(self, outbox, envelope, turn_id, *, autonomous=False, candidate=None, event_ids=()):
        self.outbox, self.envelope, self.turn_id = outbox, envelope, turn_id
        self.autonomous, self.candidate = autonomous, safe_data(candidate or {})
        self.event_ids = list(event_ids)
        self.source_message_refs = []
        self.turn_message_refs = []
        self.memory_presentations = {}
        self.confirmed_memory_presentations = {}
        self.memory_citations = []
        self.prefix = "experience:" + hashlib.sha256(str(outbox.path.resolve()).encode()).hexdigest() + ":" + turn_id

    def _record(self, key, record):
        record = {**record, "turn_id": self.turn_id, "autonomous": self.autonomous,
                  "candidate": self.candidate, "event_ids": self.event_ids,
                  "source_message_refs": self.source_message_refs}
        if record.get("category") == "turn":
            record["turn_message_refs"] = self.turn_message_refs
            record["memory_usage"] = {"presented": list(self.confirmed_memory_presentations.values()),
                                      "citations": self.memory_citations}
        self.outbox.put(key, "record_experience", {"envelope": self.envelope, "record": safe_data(record)})

    def present_memory(self, item, spans):
        nid = item.get("node_id")
        if not nid or not item.get("node_digest") or not spans:
            return
        previous = self.memory_presentations.get(nid, {})
        self.memory_presentations[nid] = {
            "node_id": nid, "node_digest": item["node_digest"],
            "spans": list(dict.fromkeys([*(previous.get("spans") or []), *spans]))[:8],
            "chain_ids": list(dict.fromkeys([*(previous.get("chain_ids") or []),
                                             *(item.get("chain_ids") or [])]))[:20],
        }

    def confirm_memory_presentations(self, messages):
        """Confirm only spans in a request for which the model returned data."""
        texts = [m.get("content", "") for m in messages if isinstance(m, dict)]
        text = "\n".join(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                         for value in texts)
        for nid, item in self.memory_presentations.items():
            if nid not in text:
                continue
            spans = [span for span in item["spans"]
                     if any(encoded in text for encoded in (
                         span, repr(span)[1:-1], json.dumps(span, ensure_ascii=False)[1:-1]))]
            if spans:
                previous = self.confirmed_memory_presentations.get(nid, {})
                if previous.get("node_digest") != item["node_digest"]:
                    previous = {}
                self.confirmed_memory_presentations[nid] = {
                    **item, "spans": list(dict.fromkeys([*(previous.get("spans") or []), *spans]))[:8],
                    "chain_ids": list(dict.fromkeys([*(previous.get("chain_ids") or []),
                                                     *(cid for cid in item["chain_ids"] if cid in text)]))[:20],
                }

    def cite_memory(self, source_id, answer_span, source_quote="", chain_id=""):
        if source_id not in self.confirmed_memory_presentations:
            raise ValueError("引用来源必须已在本轮呈现")
        if not isinstance(answer_span, str) or not answer_span.strip():
            raise ValueError("引用需要最终回答中的连续片段")
        allowed = self.confirmed_memory_presentations[source_id].get("chain_ids") or []
        if chain_id and chain_id not in allowed:
            raise ValueError("关联理由必须来自本轮实际呈现的资料")
        self.memory_citations.append({"source_id": source_id, "answer_span": answer_span,
                                      "source_quote": source_quote, "chain_id": chain_id})

    def begin(self, command, *, category="tool", key=None):
        key = key or self.prefix + ":" + uuid.uuid4().hex
        record = {"category": category, "action": command.get("action", ""),
                  "skill": command.get("skill", ""), "args": safe_data(command.get("args", {})),
                  "started_at": time.time(), "status": "started", "result": ""}
        self._record(key, record)
        return key, record

    def finish(self, entry, result="", *, status=None, activity=None):
        key, record = entry
        finished_at = time.time()
        record = {**record, "result": result_data(result), "finished_at": finished_at,
                  "duration_seconds": max(0, finished_at - record["started_at"]),
                  "status": status or outcome_status(result)}
        if record.get("category") == "turn" and activity:
            if activity.get("id") and "sequence" in activity:
                record["activity"] = safe_data(activity)
            else:
                from common.activity import activity_checkpoint
                candidate = {
                    "id": self.turn_id, "intent": self.candidate.get("intent") or "",
                    **self.candidate,
                }
                record["activity"] = activity_checkpoint(candidate, activity, [])
        self._record(key, record)

    def capture(self, action, args, result, *, status=None, category="observation", key=None, activity=None):
        if category == "turn" and key:
            with self.outbox.connection() as db:
                previous = db.execute("SELECT params FROM memory_outbox WHERE source_ref=?", (key,)).fetchone()
            if previous:
                # Terminal receipts revise the same real turn. Preserve its
                # measured duration instead of replacing it with a zero-length audit.
                record = json.loads(previous["params"])["record"]
                if record.get("status") != "started":
                    record = {**record, "result": result_data(result),
                              "status": status or outcome_status(result)}
                    if activity:
                        record["activity"] = safe_data(activity)
                    self._record(key, record)
                    return key
        entry = self.begin({"action": action, "args": args}, category=category, key=key)
        self.finish(entry, result, status=status, activity=activity)
        return entry[0]

    def execute(self, command, execute):
        entry = self.begin(command)
        try:
            result = execute()
        except BaseException as exc:
            self.finish(entry, f"{type(exc).__name__}: {exc}", status="failed")
            raise
        self.finish(entry, result)
        if command.get("action") == "update_status" and str(result).startswith("动态状态已更新"):
            self.state_updated = True
        return result

    def begin_turn(self):
        key = self.prefix + ":turn"
        with self.outbox.connection() as db:
            previous = db.execute("SELECT params FROM memory_outbox WHERE source_ref=?", (key,)).fetchone()
        if previous and json.loads(previous["params"])["record"]["status"] in {"completed", "sent"}:
            return None
        return self.begin({"action": "autonomous_turn" if self.autonomous else "dialogue_turn",
                           "args": {}}, category="turn", key=key)
