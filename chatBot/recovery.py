"""Bounded, restartable recovery of old gateway journals.

Triggers track changes after migration, including operations completed late. The
initial scan uses rowid cursors; no startup loads the full event/operation history.
"""
import json
import time


TABLES = {"event": "events", "turn": "turns", "operation": "operations"}


def initialize_recovery(store):
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_recovery_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                source_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                UNIQUE(kind,source_id)
            );
            CREATE INDEX IF NOT EXISTS memory_recovery_due ON memory_recovery_queue(next_attempt,id);
            CREATE TABLE IF NOT EXISTS memory_recovery_cursors (
                kind TEXT PRIMARY KEY, cursor INTEGER NOT NULL, upper_bound INTEGER NOT NULL
            );
        """)
        # Capture the legacy high-water marks and install all triggers under the
        # same write lock. A concurrent insert is either in the initial scan or
        # observed by a trigger; it cannot fall between those two mechanisms.
        db.execute("BEGIN IMMEDIATE")
        for kind, table in TABLES.items():
            db.execute("INSERT OR IGNORE INTO memory_recovery_cursors VALUES(?,0,?)",
                       (kind, db.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0]))
            fields = {"event": "raw,batch", "turn": "payload,status", "operation": "result,status"}[kind]
            changed = " OR ".join(f"old.{field} IS NOT new.{field}" for field in fields.split(","))
            for action, suffix in (("INSERT", "insert"), (f"UPDATE OF {fields}", "update")):
                condition = f"WHEN {changed}" if suffix == "update" else ""
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS recovery_{table}_{suffix}
                    AFTER {action} ON {table} {condition} BEGIN
                    INSERT INTO memory_recovery_queue(kind,source_id) VALUES('{kind}',new.id)
                    ON CONFLICT(kind,source_id) DO UPDATE SET revision=revision+1,attempts=0,next_attempt=0;
                    END""")


def seed_recovery(store, limit):
    """Seed at most limit rows from each old table; new changes use triggers."""
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for kind, table in TABLES.items():
            cursor = db.execute("SELECT * FROM memory_recovery_cursors WHERE kind=?", (kind,)).fetchone()
            if cursor["cursor"] >= cursor["upper_bound"]:
                continue
            rows = db.execute(f"SELECT rowid,id FROM {table} WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
                              (cursor["cursor"], cursor["upper_bound"], limit)).fetchall()
            db.executemany("INSERT OR IGNORE INTO memory_recovery_queue(kind,source_id) VALUES(?,?)",
                           [(kind, row["id"]) for row in rows])
            position = rows[-1]["rowid"] if len(rows) == limit else cursor["upper_bound"]
            db.execute("UPDATE memory_recovery_cursors SET cursor=? WHERE kind=?", (position, kind))


def event_for_turn(db, turn_id):
    # Both lookups use indexes; row order is deterministic for a merged batch.
    return db.execute("SELECT * FROM events WHERE id=?", (turn_id,)).fetchone() or db.execute(
        "SELECT * FROM events WHERE batch=? ORDER BY received,id LIMIT 1", (turn_id,)).fetchone()


def event_for_operation(db, operation_id):
    # Turn IDs may themselves contain colons. Match the longest recorded prefix.
    for offset in reversed([index for index, char in enumerate(operation_id) if char == ":"]):
        event = event_for_turn(db, operation_id[:offset])
        if event:
            return event
    return None  # Local API calls without a conversation have no legacy event.


def recover_native(journal, operation, result):
    """Reconcile a native receipt without racing a live journal completion."""
    from common.memory_journal import outcome_status, safe_data
    key = journal.prefix + ":native:" + operation["id"]
    with journal.outbox.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT params,revision FROM memory_outbox WHERE source_ref=?", (key,)).fetchone()
        params = json.loads(previous["params"]) if previous else {}
        record = params.get("record") or {}
        if previous and not record.get("legacy"):
            # Repair only a still-unfinished modern record, retaining provenance.
            if (record.get("status") not in {"started", "uncertain"} or not operation["result"]
                    or operation["status"] in {"started", "uncertain"}):
                return 0
            record = {**record, "result": safe_data(result), "status": outcome_status(result),
                      "finished_at": time.time()}
        else:
            if previous and record.get("result") == result:
                return 0
            record = {"action": operation["action"], "args": json.loads(operation["params"]),
                      "result": result, "status": outcome_status(result) if operation["result"] else "uncertain",
                      "started_at": operation["created"], "finished_at": operation["created"],
                      "legacy_operation": operation["id"], "category": "tool", "legacy": True,
                      "turn_id": journal.turn_id, "autonomous": journal.autonomous,
                      "candidate": journal.candidate, "event_ids": journal.event_ids,
                      "source_message_refs": journal.source_message_refs}
            params = {"envelope": journal.envelope}
        revision = previous["revision"] + 1 if previous else 1
        params = {**params, "record": safe_data(record), "source_ref": key, "revision": revision}
        journal.outbox.dependencies(db, key, params, replace=True)
        db.execute("""INSERT INTO memory_outbox VALUES(?, 'record_experience', ?, ?, 0, ?)
            ON CONFLICT(source_ref) DO UPDATE SET params=excluded.params,revision=excluded.revision,
            synced=0,updated=excluded.updated""",
            (key, json.dumps(params, ensure_ascii=False, allow_nan=False), revision, time.time()))
    return 1


def recover_one(hub, row):
    from common.memory_journal import MemoryJournal, outcome_status
    from chatBot.memory_hub import identity_envelope

    operation = None
    with hub.store.connection() as db:
        if row["kind"] == "event":
            event_row = db.execute("SELECT * FROM events WHERE id=?", (row["source_id"],)).fetchone()
        elif row["kind"] == "turn":
            event_row = event_for_turn(db, row["source_id"])
        else:
            operation = db.execute("SELECT * FROM operations WHERE id=?", (row["source_id"],)).fetchone()
            event_row = event_for_operation(db, row["source_id"]) if operation else None
        if not event_row:
            return 0
        event = json.loads(event_row["raw"])
        turn_id = hub.store._event_turn(event_row)
        turn = db.execute("SELECT payload FROM turns WHERE id=?", (turn_id,)).fetchone()
        # A withdrawn source must not be reintroduced during legacy migration.
        identity = hub.store.identity(event)
        if event.get("post_type") == "message" and event.get("message_id") is not None:
            external = f"{identity.account}:{identity.group or 'p:'+identity.user}:{event['message_id']}"
            recalled = db.execute("SELECT recalled FROM messages WHERE scope=? AND external_id=?",
                                  (identity.scope, external)).fetchone()
            if recalled and recalled[0]:
                return 0
    proactive = event.get("post_type") == "bines_proactive"
    journal = MemoryJournal(hub.outbox, identity_envelope(identity), turn_id,
                            autonomous=proactive, candidate=event.get("proactive"), event_ids=[event_row["id"]])
    if operation:
        result = json.loads(operation["result"]) if operation["result"] else ""
        params = json.loads(operation["params"])
        hub.remember_receipt(identity, operation["action"], params, result)
        return recover_native(journal, operation, result)

    payload = json.loads(turn["payload"] or "{}") if turn else {}
    with hub.store.connection() as db:
        # Match the source_ref primary key range instead of scanning JSON for all turns.
        entries = db.execute("SELECT source_ref FROM memory_outbox WHERE source_ref>=? AND source_ref<?",
                             (journal.prefix + ":", journal.prefix + ";")).fetchall()
    if any(not entry["source_ref"].startswith(journal.prefix + ":legacy:") for entry in entries):
        return 0
    existing = {entry["source_ref"] for entry in entries}
    items = list(payload.get("outcomes") or [])
    if proactive and payload.get("reflection"):
        items.append({"action": "autonomous_turn", "result": payload["reflection"], "status": "completed"})
    count = 0
    for index, item in enumerate(items):
        key = journal.prefix + ":legacy:" + str(index)
        if key in existing:
            continue
        record = {**item, "category": "tool", "legacy": True,
                  "status": item.get("status") or outcome_status(item.get("result")),
                  "started_at": item.get("started_at") or event_row["received"],
                  "finished_at": item.get("started_at") or event_row["received"]}
        journal._record(key, record)
        count += 1
    return count


def backfill(hub, limit=100):
    import logging
    limit = max(1, min(int(limit), 1000))
    seed_recovery(hub.store, limit)
    now = time.time()
    with hub.store.connection() as db:
        rows = db.execute("SELECT * FROM memory_recovery_queue WHERE next_attempt<=? ORDER BY id LIMIT ?",
                          (now, limit)).fetchall()
    queued, failed = 0, 0
    for row in rows:
        try:
            queued += recover_one(hub, row)
        except Exception:
            failed += 1
            logging.getLogger("bines.memory.gateway").exception("网关记忆回填暂不可用；保留任务等待重试")
            with hub.store.connection() as db:
                db.execute("UPDATE memory_recovery_queue SET attempts=attempts+1,next_attempt=? WHERE id=? AND revision=?",
                           (now + min(300, 2 ** min(row["attempts"] + 1, 8)), row["id"], row["revision"]))
        else:
            with hub.store.connection() as db:
                # A concurrent completion/update keeps its newer queue revision.
                db.execute("DELETE FROM memory_recovery_queue WHERE id=? AND revision=?", (row["id"], row["revision"]))
    return {"queued": queued, "processed": len(rows), "failed": failed}
