"""Desktop adapter for the shared proactive policy; no I/O on import."""
import json
import logging
import os
from pathlib import Path
import time
import uuid

from path_setup import ensure_project_root

ROOT = ensure_project_root(__file__, 2)
from common.proactive import ProactivePolicy, appraise_history, compact_life, draft_decision, human_ref

log = logging.getLogger("bines.bored")


def main():
    import zmq
    from config import ZMQ_HOST, ZMQ_PORTS, PRESENCE_STATE_PATH
    from common.zmq_bus import create_publisher, create_subscriber
    from memory.client import MemoryClient
    from listener_helpers import is_user_online
    from common.service_ready import mark_ready

    if os.getenv("BINES_PROACTIVE_ENABLED", "auto").lower() in {"false", "0", "off", "no"}:
        mark_ready("Bored Detector")
        log.info("自主意愿已关闭")
        import threading
        threading.Event().wait()
        return
    context = zmq.Context()
    publisher = create_publisher(context, label="Bored Detector")
    results = create_subscriber(context, "bored_result", label="Bored results")
    memory = MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"], context, timeout_ms=5000)
    directory = Path(os.getenv("BINES_MEMORY_DIR", str(ROOT / "thingking" / "memory_data")))
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "proactive_desktop.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    policy = ProactivePolicy(time.time(), state=saved)
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
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(policy.dump(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    mark_ready("Bored Detector")
    log.info("自主意愿已启用 · 桌面 · 时间、情绪与历史")
    try:
        while True:
            if results.poll(10000):
                frames = results.recv_multipart()
                result = json.loads(frames[1])
                if result.get("id") == policy.state.pending.get("id"):
                    journal = MemoryJournal(outbox, local_envelope("").to_dict(), result["id"],
                                            autonomous=True, candidate=policy.state.pending)
                    journal.capture("autonomous_feedback", {}, result, status="completed",
                                    category="feedback", key=journal.prefix + ":feedback")
                    if result.get("status") == "sent":
                        policy.state.pending["contacted"] = True
                    policy.resolve(float(result.get("timestamp") or time.time()),
                                   delivered=result.get("status") in {"sent", "completed"},
                                   uncertain=result.get("status") == "uncertain",
                                   reflection=result.get("reflection") or "")
                    save()
            now = time.time()
            if policy.state.pending and now - policy.state.pending["timestamp"] > policy.config.expiry:
                # PUB has no replay guarantees; an absent acknowledgement cannot prove no output.
                policy.resolve(now, uncertain=True)
                save()
            try:
                snapshot = memory.activity_snapshot()
                owner = snapshot.get("last_owner_activity") or {}
                available = is_user_online(PRESENCE_STATE_PATH)
                last_local = max((m.get("ts") or 0 for m in snapshot.get("messages") or []), default=0)
                ready = policy.tick(
                    now, snapshot.get("last_user"), available=available,
                    last_activity=max(float(owner.get("ts") or 0), last_local),
                    relationship_score=(snapshot.get("state") or {}).get("relationship_score", 0),
                    life=snapshot.get("life") or {},
                )
                save()
                needs_appraisal = (
                    policy.state.history_ref != policy.state.user_ref and policy.state.user_ref
                    and available and now - max(float(owner.get("ts") or 0), last_local) >= policy.config.min_idle
                    and now - policy.started >= policy.config.startup_grace
                    and now >= policy.state.next_decision
                )
                if not ready and not needs_appraisal:
                    continue
                policy.defer(now)
                save()
                from memory.channels import local_envelope
                memory_context = memory.build_context(local_envelope("").to_dict(), query="近期兴趣、情绪变化与未完成想法")
                materials = {"recent": memory_context.get("working", [])[-12:],
                             "memories": memory_context.get("recalled", [])[:5],
                             "profile": memory_context.get("profile") or {}, "bridge": memory_context.get("bridge"),
                             "state": memory_context.get("state") or {},
                             "life": compact_life(memory_context.get("life"))}
                if policy.state.history_ref != policy.state.user_ref:
                    appraisal = appraise_history(materials, policy.state, now)
                    policy.appraise(appraisal, policy.state.user_ref, now)
                    save()
                    if not appraisal:
                        continue
                if not ready:
                    continue
                kinds = policy.eligible_kinds(now, snapshot.get("last_user"))
                decision = draft_decision(materials, policy.state, now, capabilities="desktop", allowed_kinds=kinds)
                if decision and decision["kind"] not in kinds:
                    decision = None
                if not decision:
                    policy.defer(time.time())
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
                             "activity_at": float(owner.get("ts") or 0), "contact_allowed": "connection" in kinds,
                             "emotion": dict(policy.state.emotion), "willingness": dict(policy.state.drives),
                             "life": compact_life(memory_context.get("life"))}
                policy.reserve(candidate)
                save()
                publisher.send_multipart([b"bored", json.dumps(candidate, ensure_ascii=False).encode()])
                log.info("自主意图已准备 · %s", decision["topic"])
            except Exception as exc:
                policy.last_tick = time.time()
                log.warning("主动聊天检查待重试 · %s", exc)
    except KeyboardInterrupt:
        pass
    finally:
        save()
        publisher.close()
        results.close()
        context.term()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
