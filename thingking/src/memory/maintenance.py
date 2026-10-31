"""Bounded unattended recovery; source evidence stays available when a model fails."""
from __future__ import annotations

import json

RECOVERABLE = ("associate", "repair_memory", "summarize", "diary", "summarize_experience", "audit_memory")


class MemoryMaintenance:
    def __init__(self, service):
        self.service, self.repo = service, service.repo
        self.last_check = float("-inf")
        self.repo.conn.execute("""CREATE TABLE IF NOT EXISTS memory_recovery_events (
            job_id INTEGER NOT NULL, cycle INTEGER NOT NULL, recovered_at REAL NOT NULL,
            previous_error TEXT NOT NULL, PRIMARY KEY(job_id,cycle))""")

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
        }
