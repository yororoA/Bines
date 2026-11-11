"""Bounded unattended recovery; source evidence stays available when a model fails."""
from __future__ import annotations

import json
import logging
import math
import re
import hashlib

RECOVERABLE = ("associate", "repair_memory", "summarize", "diary", "summarize_experience", "audit_memory")
logger = logging.getLogger("bines.memory.maintenance")


def resume_quota_blocked_jobs(repo):
    """Release retired quota gates; preserve progress and monotonic claim tokens."""
    now = repo.clock()
    with repo.transaction():
        daily = repo.conn.execute("""UPDATE jobs SET available_at=?,lease_until=NULL,
            updated_at=?,last_error=NULL WHERE status='pending'
            AND last_error='background daily budget deferred'
            AND json_extract(payload,'$.terminal_reason') IS NULL""", (now, now)).rowcount
        calls = repo.conn.execute("""UPDATE jobs SET status='pending',available_at=?,lease_until=NULL,
            updated_at=?,last_error=NULL,payload=json_remove(payload,'$.terminal_reason')
            WHERE status='failed' AND last_error='model call budget exhausted'
            AND json_extract(payload,'$.terminal_reason')='model_call_budget'""", (now, now)).rowcount
    if daily or calls:
        logger.info("resumed quota-blocked memory jobs: daily=%d, per_job=%d", daily, calls)
    return {"daily": daily, "per_job": calls}


class MemoryMaintenance:
    def __init__(self, service):
        self.service, self.repo = service, service.repo
        self.last_check = float("-inf")
        self.repo.conn.execute("""CREATE TABLE IF NOT EXISTS memory_recovery_events (
            job_id INTEGER NOT NULL, cycle INTEGER NOT NULL, recovered_at REAL NOT NULL,
            previous_error TEXT NOT NULL, PRIMARY KEY(job_id,cycle))""")
        self.repo.conn.execute("""CREATE TABLE IF NOT EXISTS memory_manual_retries (
            job_id INTEGER NOT NULL, attempt INTEGER NOT NULL, previous_updated_at REAL NOT NULL,
            retried_at REAL NOT NULL, reason TEXT NOT NULL, previous_failure TEXT NOT NULL,
            PRIMARY KEY(job_id,attempt))""")
        resume_quota_blocked_jobs(self.repo)

    def retry_failed(self, job_id, *, scope, kind, expected_attempt, expected_updated_at,
                     expected_error_type, reason):
        """Trusted, explicit one-job recovery; never infer that an old connection label is transient."""
        if (type(job_id) is not int or job_id < 1 or type(expected_attempt) is not int or expected_attempt < 0
                or not isinstance(scope, str) or not scope or len(scope) > 256 or kind not in RECOVERABLE
                or type(expected_updated_at) not in (int, float) or not math.isfinite(expected_updated_at)
                or not isinstance(expected_error_type, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", expected_error_type)
                or not isinstance(reason, str)
                or reason not in {"connectivity_rechecked", "configuration_corrected", "request_corrected"}):
            raise ValueError("single-job retry requires an explicit scope, version, error type and safe reason code")
        with self.repo.transaction():
            job = self.repo.get_job(job_id)
            if (not job or job["status"] != "failed" or job["scope"] != scope or job["kind"] != kind
                    or job["attempts"] != expected_attempt or job["updated_at"] != expected_updated_at):
                raise ValueError("failed job no longer matches the expected scope and version")
            payload = job["payload"]
            failure = payload.get("last_failure") or {}
            causes = failure.get("causes") or []
            actual_type = causes[0] if causes else (job["last_error"] or "").split(":", 1)[0]
            if actual_type != expected_error_type or payload.get("terminal_reason") is not None:
                raise ValueError("failed job error changed or has a terminal recovery guard")
            previous = {"error_type": actual_type, "error_digest": hashlib.sha256((job["last_error"] or "").encode()).hexdigest(),
                        "generation_failures": payload.get("generation_failures", 0),
                        "recovery_cycle": payload.get("recovery_cycle", 0),
                        "transient_failures": payload.get("transient_failures", 0)}
            now = self.service.clock()
            self.repo.conn.execute("INSERT INTO memory_manual_retries VALUES(?,?,?,?,?,?)",
                                   (job_id, expected_attempt, expected_updated_at, now, reason,
                                    json.dumps(previous, ensure_ascii=False, sort_keys=True)))
            payload = {key: value for key, value in payload.items() if key not in
                       {"last_failure", "candidate_versions", "association_plan", "source_versions", "target_digest"}}
            payload.update(generation_failures=0, manual_retry_count=int(payload.get("manual_retry_count", 0)) + 1)
            # Retain recovery_cycle (and its unique audit events), lease_recoveries,
            # source snapshots and the monotonic claim token. The next claim still
            # validates current sources; a manual retry cannot revive withdrawals.
            self.repo.conn.execute("UPDATE jobs SET status='pending',available_at=?,lease_until=NULL,"
                                   "started_at=NULL,duration_seconds=NULL,last_error=NULL,payload=?,updated_at=? WHERE id=?",
                                   (now, json.dumps(payload, ensure_ascii=False), now, job_id))
            return {"status": "pending", "job_id": job_id, "attempts": job["attempts"], "available_at": now}

    def tick(self):
        now, settings = self.service.clock(), self.service.settings
        if now - self.last_check < 60:
            return 0
        self.last_check = now
        recovered = 0
        with self.repo.transaction():
            # One probe per kind in each cooldown window prevents a provider
            # outage from immediately waking the entire historical backlog.
            for kind in RECOVERABLE:
                recent = self.repo.conn.execute("""SELECT 1 FROM memory_recovery_events r
                    JOIN jobs j ON j.id=r.job_id WHERE j.kind=? AND r.recovered_at>? LIMIT 1""",
                    (kind, now - settings.job_recovery_seconds)).fetchone()
                if recent:
                    continue
                row = self.repo.conn.execute("""SELECT * FROM jobs WHERE kind=? AND status='failed'
                    AND updated_at<=?
                    AND json_extract(payload,'$.terminal_reason') IS NULL
                    AND COALESCE(json_extract(payload,'$.recovery_cycle'),0)<?
                    ORDER BY updated_at,id LIMIT 1""",
                    (kind, now - settings.job_recovery_seconds, settings.job_recovery_cycles)).fetchone()
                if not row:
                    continue
                payload = json.loads(row["payload"])
                cycle = int(payload.get("recovery_cycle", 0)) + 1
                payload = {k: v for k, v in payload.items() if k not in
                           {"candidate_versions", "association_plan", "source_versions", "target_digest"}}
                payload.update(recovery_cycle=cycle, generation_failures=0)
                self.repo.conn.execute("""INSERT INTO memory_recovery_events VALUES(?,?,?,?)""",
                    (row["id"], cycle, now, (row["last_error"] or "")[:2000]))
                self.repo.conn.execute("""UPDATE jobs SET status='pending',available_at=?,lease_until=NULL,
                    payload=?,updated_at=? WHERE id=? AND status='failed'""",
                    (now, json.dumps(payload, ensure_ascii=False), now, row["id"]))
                # attempts is a lease fencing token: never reset on recovery.
                recovered += 1
        return recovered

    def stats(self):
        return {
            "recovered": self.repo.conn.execute("SELECT COUNT(*) FROM memory_recovery_events").fetchone()[0],
            "exhausted": self.repo.conn.execute("""SELECT COUNT(*) FROM jobs WHERE status='failed'
                AND COALESCE(json_extract(payload,'$.recovery_cycle'),0)>=?""",
                (self.service.settings.job_recovery_cycles,)).fetchone()[0],
            "recovery_cycles": self.service.settings.job_recovery_cycles,
            "cooldown_seconds": self.service.settings.job_recovery_seconds,
            "lease_recovery_limit": self.service.settings.job_max_lease_recoveries,
            "budget_exhausted": self.repo.conn.execute("SELECT COUNT(*) FROM jobs WHERE status='failed' "
                "AND json_extract(payload,'$.terminal_reason')='lease_recovery_budget'").fetchone()[0],
        }
