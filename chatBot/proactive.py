"""Owner-private proactive scheduler and dispatch guard, using durable QQ receipts."""
import json
import logging
import os
import time
import uuid

from common.proactive import (
    ProactiveConfig, ProactivePolicy, appraise_history, capability_summary, compact_life,
    candidate_idle, draft_decision, human_ref, owner_contact,
)

log = logging.getLogger("bines.qq.proactive")


def enabled():
    choice = os.getenv("BINES_PROACTIVE_ENABLED", "auto").lower()
    profile = os.getenv("_BINES_LAUNCH_PROFILE") or os.getenv("BINES_PROFILE", "full")
    return choice in {"1", "true", "yes", "on"} or (choice == "auto" and profile == "chatbot")


def latest_private(store, conversation=None):
    """Only real inbound owner events establish a private recipient."""
    with store.connection() as db:
        rows = db.execute(
            "SELECT m.* FROM messages m JOIN events e ON e.id='message:'||m.external_id "
            "WHERE m.role='user' AND m.recalled=0 "
            + ("AND m.conversation=? " if conversation else "")
            + "ORDER BY m.ts DESC,m.id DESC",
            (conversation,) if conversation else (),
        ).fetchall()
    result, seen = [], set()
    for row in rows:
        event = json.loads(row["raw"] or "{}")
        identity = store.identity(event)
        if (event.get("post_type") != "message" or not identity.owner or identity.group
                or not identity.account or identity.user == identity.account
                or identity.conversation in seen):
            continue
        seen.add(identity.conversation)
        result.append((dict(row), event, identity))
    return result


def owner_activity(store):
    with store.connection() as db:
        row = db.execute(
            "SELECT MAX(ts) ts FROM messages WHERE role='user' AND recalled=0 AND actor IN ("
            + ",".join("?" for _ in store.owners) + ")", tuple(store.owners),
        ).fetchone() if store.owners else None
    return float(row["ts"] or 0) if row else 0


def guard(store, event, now=None, *, config=None, central_activity=0):
    now = time.time() if now is None else now
    config = config or ProactiveConfig.from_env()
    candidate = event.get("proactive") or {}
    from common.activity import activity_control
    if activity_control().get("mode") in {"pause", "stop"}:
        return "owner_paused"
    identity = store.identity(event)
    if not identity.owner or identity.group or identity.user == identity.account:
        return "invalid_target"
    if candidate.get("kind") == "connection" and config.quiet(now):
        return "quiet_hours"
    with store.connection() as db:
        run = db.execute("SELECT candidate FROM proactive_runs WHERE id=?", (event.get("event_id"),)).fetchone()
        if not run or json.loads(run["candidate"]) != candidate:
            return "unknown_candidate"
        execution = db.execute("SELECT status,lease FROM events WHERE id=?", (event.get("event_id"),)).fetchone()
        pending = db.execute(
            "SELECT raw FROM events WHERE conversation=? AND status IN ('pending','working')",
            (identity.conversation,),
        ).fetchall()
    active = execution and execution["status"] == "working" and execution["lease"] > now
    age = now - float(candidate.get("timestamp") or 0)
    if age < 0 or (age > config.expiry and not active):
        return "expired"
    if any(json.loads(row["raw"]).get("post_type") == "message" for row in pending):
        return "incoming_message"
    latest = latest_private(store, identity.conversation)
    if not latest or human_ref(latest[0][0]) != candidate.get("based_on_user"):
        return "new_user_message"
    user = latest[0][0]
    # A desktop or group interaction after this candidate also cancels it.
    if max(owner_activity(store), central_activity) > float(candidate.get("activity_at") or 0):
        return "owner_active_elsewhere"
    idle = candidate_idle(candidate, config)
    with store.connection() as db:
        last = db.execute("SELECT MAX(ts) FROM messages WHERE conversation=? AND recalled=0 AND ts<=?",
                          (identity.conversation, candidate["timestamp"])).fetchone()[0]
    # Do not let this quantum's own sends cancel subsequent tools/messages.
    reply_at = float(candidate.get("reply_at") or 0) if active else float(last or 0)
    if now - max(float(user["ts"]), central_activity, reply_at) < idle:
        return "not_idle"
    return ""


def dispatch_allowed(store, event, action, params, operation_id, *, central_activity=0):
    """Fresh internal intention uses the owner's native API authority and stable receipts."""
    candidate = event["proactive"]
    from chatBot.capabilities import is_read
    if not is_read(action):
        with store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM operations WHERE id LIKE ?",
                               (event["event_id"] + ":%",)).fetchone()[0]
        if count >= ProactiveConfig.from_env().quantum_actions and store.operation(operation_id).get("status") == "not_found":
            raise PermissionError("自主轮次写操作额度已用完")
    if not is_read(action) and (not operation_id or not operation_id.startswith(event["event_id"] + ":")):
        raise PermissionError("自主操作必须绑定本轮回执编号")
    # A replay of a recorded receipt is safe even if the event is now stale.
    if operation_id and store.operation(operation_id).get("status") != "not_found":
        return
    if action.startswith("delete_") or action in {"clean_cache", "logout", "set_restart", "set_group_kick", "set_group_admin"}:
        raise PermissionError("自主轮次不执行删除或更改安全设置")
    if not is_read(action) and ProactiveConfig.from_env().quiet(time.time()):
        raise PermissionError("安静时段不执行主动联系或账号写操作")
    if not candidate.get("contact_allowed") and not is_read(action):
        raise PermissionError("当前意愿轮次只允许不打扰他人的读取和观察")
    if candidate.get("planning_only"):
        raise PermissionError("当前休息思考轮次不执行 NapCat 操作")
    if (not candidate.get("owner_contact_allowed", candidate.get("contact_allowed", True))
            and owner_contact(action, params, store.owners)):
        raise PermissionError("主人私聊仍处于冷却或免打扰状态")
    reason = guard(store, event, central_activity=central_activity)
    if reason:
        raise PermissionError(f"主动消息已取消：{reason}")


class QQProactive:
    def __init__(self, store, memory, client, *, config=None, decide=draft_decision,
                 appraise=appraise_history, clock=time.time):
        self.store, self.memory, self.client = store, memory, client
        self.config, self.decide, self.clock = config or ProactiveConfig.from_env(), decide, clock
        self.appraise = appraise
        self.policies = {}
        self.capabilities = None

    def save(self, conversation, policy):
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO proactive_sessions VALUES(?,?)",
                       (conversation, json.dumps(policy.dump(), ensure_ascii=False)))
        from common.memory_journal import MemoryOutbox, MemoryJournal
        from chatBot.messages import Identity
        from chatBot.memory_hub import identity_envelope
        import hashlib
        if ":p:u:" not in conversation:
            return
        account, user = conversation[3:].split(":p:u:", 1)
        identity = Identity(account, user, owner=user in self.store.owners)
        outbox = MemoryOutbox(self.store.path)
        for outcome in policy.state.recent_outcomes:
            digest = hashlib.sha256(json.dumps(outcome, sort_keys=True).encode()).hexdigest()
            ref = "qq-feedback:" + conversation + ":" + digest
            with outbox.connection() as db:
                exists = db.execute("SELECT 1 FROM memory_outbox WHERE source_ref=?", (ref,)).fetchone()
            if not exists:
                journal = MemoryJournal(outbox, identity_envelope(identity, timestamp=outcome.get("at")),
                                        ref, autonomous=True, candidate={"topic": outcome.get("topic", "")})
                journal.capture("autonomous_feedback", {}, outcome, category="feedback",
                                status="completed", key=ref)

    def recover(self, policy, now):
        candidate = policy.state.pending
        if not candidate:
            return
        self._recover(policy, now)

    def consume_handoffs(self, conversation, policy, now):
        """Scheduler owns policy state; owner workers only append durable handoffs."""
        if policy.state.pending:
            return
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM activity_handoffs WHERE conversation=? AND consumed=0 ORDER BY created,rowid",
                (conversation,),
            ).fetchall()
            if not rows:
                return
            checkpoint = json.loads(rows[-1]["checkpoint"])
            policy.state.activity = {} if checkpoint.get("mode") == "finish" else checkpoint
            policy.state.next_decision = checkpoint.get("next_at", now)
            # Persist state and acknowledgment in one commit, even across a restart.
            db.execute("INSERT OR REPLACE INTO proactive_sessions VALUES(?,?)",
                       (conversation, json.dumps(policy.dump(), ensure_ascii=False)))
            db.executemany("UPDATE activity_handoffs SET consumed=1 WHERE turn_id=?",
                           [(row["turn_id"],) for row in rows])
        log.info("活动进度已接手 · %s · 第%s批", checkpoint.get("topic", ""),
                 checkpoint.get("sequence", 0))

    def _recover(self, policy, now):
        candidate = policy.state.pending
        with self.store.connection() as db:
            run = db.execute("SELECT * FROM proactive_runs WHERE id=?", (candidate["id"],)).fetchone()
            operations = db.execute("SELECT * FROM operations WHERE id LIKE ?",
                                    (candidate["id"] + ":%",)).fetchall()
            turn = db.execute("SELECT payload FROM turns WHERE id=?", (candidate["id"],)).fetchone()
            execution = db.execute("SELECT status,lease FROM events WHERE id=?", (candidate["id"],)).fetchone()
        if (run and run["status"] == "queued" and execution
                and execution["status"] == "working" and execution["lease"] > now):
            return  # A renewed worker lease covers slow browser/model calls.
        reflection = (json.loads(turn["payload"] or "{}").get("reflection", "") if turn else "")
        checkpoint = (json.loads(turn["payload"] or "{}").get("activity") if turn else None)
        if run and run["status"] == "queued":
            with self.store.connection() as db:
                event = db.execute("SELECT 1 FROM events WHERE id=?", (candidate["id"],)).fetchone()
            if not event:
                policy.resolve(now)
                with self.store.connection() as db:
                    db.execute("UPDATE proactive_runs SET status='skipped',error='interrupted_before_queue' WHERE id=?",
                               (candidate["id"],))
                return
        delivered = next((op for op in operations if op["status"] == "ok"
                          and json.loads(op["result"] or "{}").get("retcode", 0) == 0), None)
        from chatBot.messages import SEND_ACTIONS
        contacted = any(op["action"] in SEND_ACTIONS for op in operations)
        if contacted:
            policy.state.pending["contacted"] = True
        policy.state.pending["owner_contacted"] = any(
            owner_contact(op["action"], json.loads(op["params"] or "{}"), self.store.owners)
            for op in operations)
        terminal = run and run["status"] != "queued"
        if (delivered and (terminal or now - candidate["timestamp"] > self.config.expiry)
                or (run and run["status"] in {"sent", "completed"})):
            policy.resolve(float(run["delivered_at"] or delivered["created"]) if run else float(delivered["created"]),
                           delivered=True, reflection=reflection, activity=checkpoint)
        elif run and run["status"] == "uncertain":
            policy.resolve(now, uncertain=True, activity=checkpoint)
        elif not run or run["status"] in {"skipped", "failed"}:
            policy.resolve(now, activity=checkpoint)
        elif now - candidate["timestamp"] > self.config.expiry:
            # Dispatch already began but never committed a receipt: don't chase/replay.
            interrupted = bool(self.store.turn_writes(candidate["id"]))
            if not checkpoint and (candidate.get("activity") or interrupted):
                from common.activity import activity_checkpoint
                checkpoint = activity_checkpoint(candidate, {
                    "mode": "pause" if interrupted else "review",
                    "delay_seconds": 0 if interrupted else 15,
                    "note": "批次中断，重新观察；已有操作需先核查回执，禁止重放",
                }, [])
            policy.resolve(now, uncertain=interrupted, activity=checkpoint)
            with self.store.connection() as db:
                db.execute("UPDATE proactive_runs SET status=? WHERE id=?",
                           ("uncertain" if interrupted else "skipped", candidate["id"]))

    def tick(self):
        now = self.clock()
        snapshot = self.memory.activity_snapshot()
        central = snapshot.get("last_owner_activity") or {}
        activity = max(owner_activity(self.store), float(central.get("ts") or 0))
        health = self.client.request("/health", {})
        connected = isinstance(health, dict) and health.get("connected") is True
        for user, original, identity in latest_private(self.store):
            conversation = identity.conversation
            policy = self.policies.get(conversation)
            if policy is None:
                with self.store.connection() as db:
                    row = db.execute("SELECT value FROM proactive_sessions WHERE conversation=?", (conversation,)).fetchone()
                policy = self.policies[conversation] = ProactivePolicy(
                    now, self.config, json.loads(row["value"]) if row else None,
                )
            self.recover(policy, now)
            self.consume_handoffs(conversation, policy, now)
            with self.store.connection() as db:
                busy = db.execute(
                    "SELECT 1 FROM events WHERE conversation=? AND status IN ('pending','working')",
                    (conversation,),
                ).fetchone() is not None
                last = db.execute("SELECT MAX(ts) ts FROM messages WHERE conversation=?", (conversation,)).fetchone()
            conversation_activity = max(activity, float(last["ts"] or 0))
            ready = policy.tick(
                now, user, available=connected and not busy,
                last_activity=activity, last_reply_at=conversation_activity,
                followup_after_reply=not busy,
                relationship_score=(snapshot.get("state") or {}).get("relationship_score", 0),
                life=snapshot.get("life") or {},
            )
            self.save(conversation, policy)  # Save observation even during cooldown/quiet.
            if not ready:
                continue
            policy.defer(now)  # Failed model calls also back off, rather than retrying every tick.
            self.save(conversation, policy)
            from chatBot.memory_hub import identity_envelope
            continuing = policy.state.activity.get("mode") in {"continue", "review"}
            memory_context = (snapshot if continuing else self.memory.build_context(
                identity_envelope(identity), query="近期兴趣、情绪变化与未完成的想法"))
            materials = {
                "recent": memory_context.get("working", [])[-12:],
                "memories": memory_context.get("recalled", [])[:5],
                "profile": memory_context.get("profile") or {},
                "bridge": memory_context.get("bridge"),
                "state": memory_context.get("state") or {},
                "life": compact_life(memory_context.get("life") or snapshot.get("life")),
                "recent_outcomes": policy.state.recent_outcomes,
                "planning_only": policy.state.planning_only,
                **policy.contact_permissions(now, user, activity, last_reply_at=conversation_activity),
            }
            if self.capabilities is None:
                self.capabilities = capability_summary("QQ")
            materials["available_skills"] = self.capabilities
            needs_appraisal = (policy.appraisal_due(human_ref(user), now)
                               and (not continuing or bool(policy.state.emotion_at)))
            if needs_appraisal:
                appraisal = self.appraise(materials, policy.state, now)
                policy.appraise(appraisal, human_ref(user), now)
                self.save(conversation, policy)
            kinds = policy.eligible_kinds(now, user, activity, last_reply_at=conversation_activity)
            saved_activity = policy.state.activity
            continuing = saved_activity.get("mode") in {"continue", "review"}
            if continuing:
                decision = {"kind": saved_activity["kind"], "topic": saved_activity["topic"],
                            "intent": saved_activity["intent"], "reason": saved_activity["reason"]}
            else:
                decision = self.decide(materials, policy.state, now, capabilities="QQ", allowed_kinds=kinds)
                decision = policy.record_decision(decision, now)
            if decision and not continuing and decision["kind"] not in kinds:
                policy.state.last_decision.update(status="disallowed_kind", proposed=decision)
                decision = None
            if not decision:
                policy.defer(self.clock(), policy.state.last_decision.get("delay_seconds"))
                self.save(conversation, policy)
                continue
            event_id = "proactive:" + uuid.uuid4().hex
            candidate = {**decision, "id": event_id, "based_on_user": human_ref(user),
                         "activity_at": activity, "reply_at": conversation_activity, "timestamp": self.clock(),
                         **policy.contact_permissions(now, user, activity, last_reply_at=conversation_activity),
                         "planning_only": policy.state.planning_only,
                         "activity": dict(saved_activity) if continuing else {},
                         "emotion": dict(policy.state.emotion), "willingness": dict(policy.state.drives),
                         "life": materials["life"]}
            event = {"post_type": "bines_proactive", "self_id": original["self_id"],
                     "user_id": original["user_id"], "message_type": "private",
                     "sender": original.get("sender") or {}, "event_id": event_id, "proactive": candidate}
            with self.store.connection() as db:
                db.execute("INSERT INTO proactive_runs(id,conversation,candidate) VALUES(?,?,?)",
                           (event_id, conversation, json.dumps(candidate, ensure_ascii=False)))
            # Model calls can take seconds; check again using current central activity.
            latest = self.memory.activity_snapshot().get("last_owner_activity") or {}
            reason = guard(self.store, event, self.clock(), config=self.config,
                           central_activity=float(latest.get("ts") or 0))
            if reason:
                policy.state.last_decision.update(status="cancelled", cancellation_reason=reason)
                with self.store.connection() as db:
                    db.execute("UPDATE proactive_runs SET status='skipped',error=? WHERE id=?", (reason, event_id))
                policy.defer(self.clock())
            else:
                policy.reserve(candidate)
                self.save(conversation, policy)  # Reserve durably before exposing the event.
                with self.store.connection() as db:
                    db.execute("INSERT INTO events(id,conversation,raw,received) VALUES(?,?,?,?)",
                               (event_id, conversation, json.dumps(event, ensure_ascii=False), self.clock()))
                log.info("自主意图已准备 · %s", decision["topic"])
            self.save(conversation, policy)

    def run(self, stop):
        log.info("自主意愿已启用 · 时间、情绪与交流历史")
        while not stop.wait(2):
            try:
                self.tick()
            except Exception as exc:
                # A failed observation never contributes elapsed time on recovery.
                for policy in self.policies.values():
                    policy.last_tick = self.clock()
                log.warning("主动聊天检查待重试 · %s", exc)
