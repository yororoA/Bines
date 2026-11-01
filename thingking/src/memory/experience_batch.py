"""Bounded same-scope experience requests; each source keeps its own receipt."""
import json
import os


def collect(service, job, materials):
    repo, now = service.repo, service.clock()
    maximum = max(1, min(8, int(os.environ.get("BINES_MEMORY_EXPERIENCE_BATCH_SIZE", "6"))))
    wait = max(0, min(300, int(os.environ.get("BINES_MEMORY_EXPERIENCE_BATCH_WAIT", "60"))))
    if maximum == 1 or job["payload"].get("generation_failures"):
        return materials
    record = materials["record"]
    turn_id = record.get("turn_id")
    activity_id = (record.get("activity") or {}).get("id")
    # Never combine unrelated callers or anonymous legacy receipts merely
    # because their timestamps or tool names happen to match.
    if not turn_id and not activity_id:
        return materials
    with repo.transaction():
        candidates = repo.conn.execute("""SELECT j.id FROM jobs j JOIN experiences e
            ON json_extract(j.payload,'$.source_ref')=e.source_ref
            AND json_extract(j.payload,'$.revision')=e.revision
            JOIN memories m ON m.id=e.memory_id
            WHERE j.kind='summarize_experience' AND j.status='pending' AND j.available_at<=?
            AND COALESCE(json_extract(j.payload,'$.generation_failures'),0)=0
            AND j.scope=? AND j.conversation_id IS ? AND m.deleted=0
            AND m.day_key=(SELECT day_key FROM memories WHERE id=
                (SELECT memory_id FROM experiences WHERE source_ref=?))
            AND ((? IS NOT NULL AND json_extract(e.record,'$.turn_id')=?)
                OR (? IS NOT NULL AND json_extract(e.record,'$.activity.id')=?))
            ORDER BY j.created_at,j.id LIMIT ?""",
            (now, job["scope"], job["conversation_id"], job["payload"]["source_ref"],
             turn_id, turn_id, activity_id, activity_id, maximum - 1)).fetchall()
        if len(candidates) + 1 < maximum and now < job["created_at"] + wait:
            repo.conn.execute("UPDATE jobs SET status='pending',lease_until=NULL,available_at=? WHERE id=?",
                              (job["created_at"] + wait, job["id"]))
            return False
        members = [job]
        records = [{"source_ref": job["payload"]["source_ref"], "record": record}]
        size = len(json.dumps(records, ensure_ascii=False).encode())
        for row in candidates:
            other = repo.get_job(row["id"])
            # Claim with a fresh fencing token before creating digest-bound material.
            repo.conn.execute("""UPDATE jobs SET status='running',attempts=attempts+1,
                lease_until=?,started_at=?,duration_seconds=NULL,updated_at=? WHERE id=? AND status='pending'""",
                (now + service.settings.job_lease_seconds, now, now, other["id"]))
            other = repo.get_job(other["id"])
            material = service._job_materials(other)
            if not material:
                repo.finish_job(other["id"])
                continue
            item = {"source_ref": other["payload"]["source_ref"], "record": material["record"]}
            item_size = len(json.dumps(item, ensure_ascii=False).encode())
            if size + item_size > 48000:
                repo.conn.execute("UPDATE jobs SET status='pending',lease_until=NULL WHERE id=?", (other["id"],))
                break
            records.append(item)
            members.append(other)
            size += item_size
        if len(members) == 1:
            return materials
        return {**materials, "records": records, "batch_jobs": members}
