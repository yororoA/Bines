"""Durable, sender-scoped follow-up waits; never sleep while owning a worker."""
from __future__ import annotations

import json
import math
import time

from chatBot.config import FOLLOWUP_WAIT_SECONDS, FOLLOWUP_WAIT_TOTAL, FOLLOWUP_WAIT_REQUESTS


class FollowupArrived(RuntimeError):
    """The current reply snapshot is stale; its real input remains in the queue."""


class TurnDeferred(RuntimeError):
    """A deliberate wait released its durable queue claim without a failure."""


def human_input(event):
    return (event.get("post_type") == "message" and event.get("message_id") is not None
            and str(event.get("user_id") or "") not in {"", str(event.get("self_id") or "")})


class TurnWaits:
    def __init__(self, store, *, step=FOLLOWUP_WAIT_SECONDS, total=FOLLOWUP_WAIT_TOTAL,
                 requests=FOLLOWUP_WAIT_REQUESTS):
        self.store, self.step, self.total, self.requests = store, step, total, requests
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS qq_turn_waits (
                batch TEXT PRIMARY KEY, conversation TEXT NOT NULL,
                started_at REAL NOT NULL, deadline REAL NOT NULL,
                wait_count INTEGER NOT NULL DEFAULT 0, until REAL NOT NULL DEFAULT 0,
                merge_pending INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT ''
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS qq_turn_wait_due ON qq_turn_waits(merge_pending,until)")

    @staticmethod
    def _incoming(db, conversation, after):
        rows = db.execute("SELECT rowid AS queue_seq,* FROM events WHERE conversation=? AND rowid>? "
                          "AND status='pending' ORDER BY received,rowid", (conversation, after)).fetchall()
        result = []
        for row in rows:
            event = json.loads(row["raw"])
            if not human_input(event):
                continue
            # A recalled follow-up must not keep cancelling an otherwise valid reply.
            recalled = db.execute("SELECT recalled FROM messages WHERE conversation=? AND external_id LIKE ?",
                                  (conversation, "%:" + str(event["message_id"]))).fetchone()
            if not recalled or not recalled["recalled"]:
                result.append(row)
        return result

    def register(self, db, batch, rows):
        if not rows or not all(human_input(json.loads(row["raw"])) for row in rows):
            return
        first = min(float(row["received"]) for row in rows)
        db.execute("INSERT OR IGNORE INTO qq_turn_waits(batch,conversation,started_at,deadline) VALUES(?,?,?,?)",
                   (batch, rows[0]["conversation"], first, first + self.total))

    def state(self, batch):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM qq_turn_waits WHERE batch=?", (batch,)).fetchone()
        return dict(row) if row else {}

    def available(self, batch):
        state = self.state(batch)
        return bool(state and time.time() < state["deadline"] and state["wait_count"] < self.requests
                    and not self.store.turn_writes(batch))

    def new_input(self, event_id):
        with self.store.connection() as db:
            row = db.execute("SELECT rowid AS queue_seq,* FROM events WHERE id=?", (event_id,)).fetchone()
            if not row or not human_input(json.loads(row["raw"])):
                return False
            newest = (db.execute("SELECT MAX(rowid) FROM events WHERE batch=?", (row["batch"],)).fetchone()[0]
                      if row["batch"] else row["queue_seq"])
            return bool(self._incoming(db, row["conversation"], newest))

    def refresh_due(self, db, now, debounce):
        for state in db.execute("SELECT * FROM qq_turn_waits WHERE merge_pending=1").fetchall():
            newest = db.execute("SELECT MAX(rowid) FROM events WHERE batch=?", (state["batch"],)).fetchone()[0]
            if newest is None:
                continue
            incoming = self._incoming(db, state["conversation"], newest)
            due = min(state["until"], state["deadline"])
            if incoming:
                due = min(due, max(float(row["received"]) for row in incoming) + debounce)
            db.execute("UPDATE events SET next_attempt=? WHERE batch=? AND status='pending'",
                       (max(now, due), state["batch"]))

    def merge(self, db, batch, rows, limit):
        state = db.execute("SELECT * FROM qq_turn_waits WHERE batch=?", (batch,)).fetchone()
        if not state or not state["merge_pending"]:
            return rows
        incoming = self._incoming(db, state["conversation"], max(row["queue_seq"] for row in rows))
        # Only brand-new human events join a waiting batch, never another retry or Will event.
        additions = [row for row in incoming if not row["batch"]][:limit]
        if any(row["response_requested"] for row in rows):
            trigger = next(row["response_requested"] for row in rows if row["response_requested"])
            for row in additions:
                db.execute("UPDATE events SET response_requested=? WHERE id=?", (trigger, row["id"]))
        db.execute("UPDATE qq_turn_waits SET merge_pending=0,until=0 WHERE batch=?", (batch,))
        return sorted([*rows, *additions], key=lambda row: (row["received"], row["queue_seq"]))

    @staticmethod
    def _claims_current(db, claims):
        for claim in claims:
            row = db.execute("SELECT status,worker,attempts,lease FROM events WHERE id=?", (claim["id"],)).fetchone()
            if (not row or row["status"] != "working" or row["worker"] != claim["worker"]
                    or row["attempts"] != claim["attempts"] or row["lease"] <= time.time()):
                return False
        return bool(claims)

    def defer(self, claims, *, seconds=0, reason="", semantic=False):
        now, batch = time.time(), claims[0]["batch"]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._claims_current(db, claims):
                raise RuntimeError("qq_lease_lost")
            state = db.execute("SELECT * FROM qq_turn_waits WHERE batch=?", (batch,)).fetchone()
            if not state or self.store._turn_writes(db, batch):
                return False
            if semantic and (state["wait_count"] >= self.requests or now >= state["deadline"]):
                return False
            due = min(now + min(seconds, self.step), state["deadline"]) if semantic else now
            db.execute("UPDATE qq_turn_waits SET wait_count=wait_count+?,until=?,merge_pending=1,reason=? WHERE batch=?",
                       (int(semantic), due, reason[:200], batch))
            db.execute("DELETE FROM turns WHERE id=? AND status IN ('started','ready')", (batch,))
            trigger = next((row.get("_response_trigger") or row.get("response_requested")
                            for row in claims if row.get("_response_trigger") or row.get("response_requested")), 0)
            for claim in claims:
                # Attempts fence stale workers and must remain monotonic, even for the same worker name.
                db.execute("UPDATE events SET status='pending',worker=NULL,lease=0,deferred_claims=deferred_claims+1,"
                           "next_attempt=?,response_requested=?,error=NULL,failure_reason=NULL WHERE id=?",
                           (due, trigger, claim["id"]))
        return True

    def supersede(self, claims):
        """Restart only an untouched turn; completed effects remain owned by its old batch."""
        if self.defer(claims, reason="new_input"):
            return "merged"
        batch = claims[0]["batch"]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._claims_current(db, claims):
                raise RuntimeError("qq_lease_lost")
            newest = db.execute("SELECT MAX(rowid) FROM events WHERE batch=?", (batch,)).fetchone()[0]
            trigger = next((row.get("_response_trigger") or row.get("response_requested")
                            for row in claims if row.get("_response_trigger") or row.get("response_requested")), 0)
            for row in self._incoming(db, claims[0]["conversation"], newest):
                db.execute("UPDATE events SET response_requested=? WHERE id=?", (trigger, row["id"]))
            db.execute("UPDATE turns SET status='done',payload=? WHERE id=?",
                       (json.dumps({"disposition": "superseded_by_new_input", "preserved_receipts": True}), batch))
            for claim in claims:
                db.execute("UPDATE events SET status='done',worker=NULL,lease=0,next_attempt=0,error=NULL,"
                           "failure_reason=NULL WHERE id=?", (claim["id"],))
        return "completed_with_receipts"


class FollowupRunner:
    """One runtime control tool on an ordinary QQ turn, without a separate model call."""
    action = "wait_for_followup"

    def __init__(self, runner, waits, claims, check):
        self.runner, self.waits, self.claims, self.check = runner, waits, claims, check
        self.action_to_skill = {**runner.action_to_skill, self.action: "qq_turn"}

    def __getattr__(self, name):
        return getattr(self.runner, name)

    def function_tools(self, *args, **kwargs):
        tools = self.runner.function_tools(*args, **kwargs)
        # Keep the ordinary QQ tool prefix stable. Runtime eligibility is enforced in execute().
        return [*tools, {"type": "function", "function": {
                "name": self.action,
                "description": "当前同一人似乎还没说完时，暂不回答，短等后续输入；完整问题直接回应，不能用于等任务、工具或对方回复。",
                "parameters": {"type": "object", "properties": {
                    "seconds": {"type": "number", "minimum": 1, "maximum": self.waits.step},
                    "reason": {"type": "string", "description": "哪一处真实输入还未完整"},
                }, "required": ["seconds", "reason"], "additionalProperties": False},
            }}]

    def instructions_for_source(self, *args, **kwargs):
        rules = self.runner.instructions_for_source(*args, **kwargs)
        return rules + (
            "\n本轮是已收到的连续输入。明确完整的内容正常回应，不统一拖延；"
            "如果语义明显未完、对方说还要补充，可在任何工具动作和发送之前调用wait_for_followup。"
            "它只短等同一人接着输入，不是等待回复、猜测对方在打字或轮询；调用后本轮立即让出。"
            f"每次最多等{self.waits.step:g}秒，从首次输入起最多{self.waits.total:g}秒、最多{self.waits.requests}次。"
            "本轮末尾提供剩余等待状态；没有后续或次数/窗口已用完就依据现有内容回应，不编造补充，不继续请求等待。"
        )

    def prompt_state(self):
        state = self.waits.state(self.claims[0]["batch"])
        return {"remaining_seconds": round(max(0, state.get("deadline", 0) - time.time()), 1),
                "remaining_requests": max(0, self.waits.requests - state.get("wait_count", 0)),
                "available": self.waits.available(self.claims[0]["batch"])}

    def before_model_request(self):
        reason = self.check()
        if reason:
            raise RuntimeError(reason)
        callback = getattr(self.runner, "before_model_request", None)
        if callback:
            callback()

    def execute(self, command, **kwargs):
        reason = self.check()
        if reason:
            raise RuntimeError(reason)
        if command.get("action") != self.action:
            return self.runner.execute(command, **kwargs)
        args = command.get("args")
        if (not isinstance(args, dict) or set(args) != {"seconds", "reason"}
                or type(args["seconds"]) not in {int, float} or not math.isfinite(args["seconds"])
                or not 1 <= args["seconds"] <= self.waits.step
                or not isinstance(args["reason"], str) or not 2 <= len(args["reason"].strip()) <= 200):
            return json.dumps({"status": "failed", "error": "等待需有效秒数及具体的未完输入依据"}, ensure_ascii=False)
        if not self.waits.defer(self.claims, seconds=args["seconds"], reason=args["reason"], semantic=True):
            return json.dumps({"status": "unavailable", "error": "已执行动作或等待窗口/次数已用完；按现有输入回应"}, ensure_ascii=False)
        raise TurnDeferred("waiting_for_followup")
