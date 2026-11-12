"""Desktop adapter for the shared proactive policy; no I/O on import."""
import json
import logging
import os
from pathlib import Path
import time
import uuid

from path_setup import ensure_project_root

ROOT = ensure_project_root(__file__, 2)
from common.proactive import ProactivePolicy, appraise_history, capability_summary, compact_life, draft_decision, human_ref

log = logging.getLogger("bines.bored")


def generate_intention(policy, memory, snapshot, capabilities, now, save):
    from memory.channels import local_envelope
    owner = snapshot.get("last_owner_activity") or {}
    user = snapshot.get("last_user")
    last_reply = max(float(owner.get("ts") or 0),
                     max((m.get("ts") or 0 for m in snapshot.get("messages") or []), default=0))
    policy.defer_generation(now)
    save()
    permissions = policy.contact_permissions(now, user, float(owner.get("ts") or 0), last_reply_at=last_reply)
    context = memory.build_context(local_envelope("").to_dict(), query=policy.deliberation_query(
        user, contact_allowed=permissions["owner_contact_allowed"], life=snapshot.get("life")))
    materials = {
        "envelope": context.get("envelope") or local_envelope("").to_dict(),
        "recent": context.get("working", [])[-12:], "memories": context.get("recalled", [])[:5],
        "profile": context.get("profile") or {}, "bridge": context.get("bridge"),
        "state": context.get("state") or {}, "life": compact_life(context.get("life") or snapshot.get("life")),
        "available_skills": capabilities, "activity": dict(policy.state.activity),
        "ongoing_intention": {key: policy.state.pending.get(key) for key in ("kind", "topic", "intent")},
        "queued_intentions": policy.queued_intentions(), "waiting_intentions": policy.waiting_intentions(),
        "planning_only": policy.state.planning_only, **permissions,
    }
    if policy.appraisal_due(policy.state.user_ref, now):
        policy.appraise(appraise_history(materials, policy.state, now), policy.state.user_ref, now)
        save()
    kinds = policy.eligible_kinds(now, user, float(owner.get("ts") or 0), last_reply_at=last_reply)
    decision = policy.record_decision(draft_decision(
        materials, policy.state, now, capabilities="desktop", allowed_kinds=kinds), now)
    if decision and decision["kind"] not in kinds:
        policy.state.last_decision.update(status="disallowed_kind", proposed=decision)
        decision = None
    if decision and policy.enqueue_intention(decision, time.time()):
        log.info("自主意图已入队 · %s · 等待执行通道", decision["topic"])
    else:
        result = policy.state.last_decision
        policy.defer_generation(time.time(), result.get("delay_seconds", policy.config.decision_interval),
                                wait_scope=result.get("wait_scope", "review"))
    policy.state.next_intention_at = max(policy.state.next_intention_at,
                                        time.time() + policy.config.decision_interval)
    save()


def main():
    import threading
    from common.activity import activity_store
    from common.autonomy_store import AutonomyStoreError
    if os.getenv("BINES_PROACTIVE_ENABLED", "auto").lower() in {"false", "0", "off", "no"}:
        from common.service_ready import mark_ready
        mark_ready("Bored Detector")
        log.info("自主意愿已关闭")
        threading.Event().wait()
        return
    failures = 0
    while True:
        try:
            with activity_store().scheduler("desktop"):
                _run()
            return
        except AutonomyStoreError as exc:
            failures += 1
            delay = min(60, 2 ** min(failures, 6))
            log.warning("桌面自主状态待恢复 · %s · %ss 后重试", exc, delay)
            threading.Event().wait(delay)


def _run():
    import zmq
    from config import ZMQ_HOST, ZMQ_PORTS, PRESENCE_STATE_PATH
    from common.zmq_bus import create_publisher, create_subscriber
    from memory.client import MemoryClient
    from listener_helpers import is_user_online
    from common.service_ready import mark_ready

    from common.activity import activity_store, desktop_state
    from common.autonomy_store import AutonomyStoreError
    states = activity_store()
    saved, revision = desktop_state()
    context = zmq.Context()
    publisher = create_publisher(context, label="Bored Detector")
    results = create_subscriber(context, "bored_result", label="Bored results")
    memory = MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"], context, timeout_ms=5000)
    directory = Path(os.getenv("BINES_MEMORY_DIR", str(ROOT / "thingking" / "memory_data")))
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    policy = ProactivePolicy(time.time(), state=saved)
    capabilities = capability_summary("desktop")
    from common.memory_journal import MemoryOutbox, MemoryJournal
    from memory.channels import local_envelope
    outbox = MemoryOutbox(directory / "desktop_outbox.db")
    outbox.start(memory)
    for index, outcome in enumerate(saved.get("recent_outcomes") or []):
        ref = f"desktop-feedback:{outcome.get('at')}:{index}"
        with outbox.connection() as db:
            exists = db.execute("SELECT 1 FROM memory_outbox WHERE source_ref=?", (ref,)).fetchone()
        if not exists:
            journal = MemoryJournal(outbox, local_envelope("", timestamp=outcome.get("at")).to_dict(),
                                    ref, autonomous=True, candidate={"topic": outcome.get("topic", "")})
            journal.capture("autonomous_feedback", {}, outcome, status="completed", category="feedback", key=ref)

    def save():
        nonlocal revision
        revision = states.put("desktop", policy.dump(), revision)

    mark_ready("Bored Detector")
    log.info("自主意愿已启用 · 桌面 · 时间、情绪与历史")
    try:
        while True:
            if results.poll(2000):
                frames = results.recv_multipart()
                result = json.loads(frames[1])
                if result.get("id") == policy.state.pending.get("id"):
                    if result.get("status") == "working":
                        policy.state.pending["lease_until"] = float(result["timestamp"]) + policy.config.expiry
                        save()
                    else:
                        journal = MemoryJournal(outbox, local_envelope("").to_dict(), result["id"],
                                                autonomous=True, candidate=policy.state.pending)
                        journal.capture("autonomous_feedback", {}, result, status="completed",
                                        category="feedback", key=journal.prefix + ":feedback")
                        if result.get("status") == "sent":
                            policy.state.pending["contacted"] = True
                        if "owner_contacted" in result:
                            policy.state.pending["owner_contacted"] = result["owner_contacted"]
                        policy.resolve(float(result.get("timestamp") or time.time()),
                                       delivered=result.get("status") in {"sent", "completed"},
                                       uncertain=result.get("status") == "uncertain",
                                       reflection=result.get("reflection") or "",
                                       activity=result.get("activity"))
                        save()
            now = time.time()
            if (policy.state.pending and
                    now > max(policy.state.pending["timestamp"] + policy.config.expiry,
                              policy.state.pending.get("lease_until", 0))):
                # PUB has no replay guarantees; an absent acknowledgement cannot prove no output.
                policy.resolve(now, uncertain=True)
                save()
            try:
                snapshot = memory.activity_snapshot()
                from common.activity import consume_desktop_handoff
                if consume_desktop_handoff(policy):
                    save()  # State and handoff_ref share the same database document.
                owner = snapshot.get("last_owner_activity") or {}
                available = is_user_online(PRESENCE_STATE_PATH)
                last_local = max((m.get("ts") or 0 for m in snapshot.get("messages") or []), default=0)
                conversation_activity = max(float(owner.get("ts") or 0), last_local)
                ready = policy.tick(
                    now, snapshot.get("last_user"), available=available,
                    last_activity=float(owner.get("ts") or 0), last_reply_at=conversation_activity,
                    relationship_score=(snapshot.get("state") or {}).get("relationship_score", 0),
                    life=snapshot.get("life") or {},
                )
                save()
                if policy.generation_due(now, available=available):
                    generate_intention(policy, memory, snapshot, capabilities, now, save)
                if not ready:
                    continue
                if time.time() < policy.state.intention_wait_until:
                    continue
                decision, active = policy.select_intention(
                    time.time(), snapshot.get("last_user"), float(owner.get("ts") or 0),
                    last_reply_at=conversation_activity)
                continuing = bool(active)
                if not decision:
                    save()
                    continue
                latest = memory.activity_snapshot()
                if (human_ref(latest.get("last_user")) != policy.state.user_ref
                        or (latest.get("last_owner_activity") or {}).get("ts") != owner.get("ts")
                        or not is_user_online(PRESENCE_STATE_PATH)):
                    policy.defer(time.time())
                    save()
                    continue
                candidate = {**decision, "id": uuid.uuid4().hex, "type": "bored",
                             "timestamp": time.time(), "based_on_user": policy.state.user_ref,
                             "activity_at": float(owner.get("ts") or 0),
                             "reply_at": conversation_activity,
                             **policy.contact_permissions(now, snapshot.get("last_user"), float(owner.get("ts") or 0),
                                                          last_reply_at=conversation_activity),
                             "planning_only": policy.state.planning_only,
                             "activity": dict(active) if continuing else {},
                             "emotion": dict(policy.state.emotion), "willingness": dict(policy.state.drives),
                             "life": compact_life(snapshot.get("life"))}
                policy.reserve(candidate)
                save()
                publisher.send_multipart([b"bored", json.dumps(candidate, ensure_ascii=False).encode()])
                log.info("自主意图已准备 · %s", decision["topic"])
            except Exception as exc:
                policy.last_tick = time.time()
                if isinstance(exc, AutonomyStoreError):
                    raise
                log.warning("主动聊天检查待重试 · %s", exc)
    except KeyboardInterrupt:
        pass
    finally:
        outbox.stop.set()
        if outbox.thread:
            outbox.thread.join(timeout=8)
        publisher.close()
        results.close()
        context.term()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
