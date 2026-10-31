"""Bounded foreground bookkeeping; a worker owns slow embedding/Chroma batches.

Only the RPC thread touches SQLite. Completed work is acknowledged against the
source snapshot and index generation; stale work remains pending for repair.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("bines.memory.vector_maintenance")


class VectorMaintenance:
    def __init__(self, service, *, time_budget_ms=10, batch_size=None):
        self.service, self.repo, self.index = service, service.repo, service.index
        self.keys = service.retriever.keys
        self.batch_size = min(64, max(1, batch_size or service.settings.index_batch_size))
        self.time_budget_ms = max(1, time_budget_ms)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-vectors")
        self._future = None
        self._snapshot = None
        self._requested = True
        self._cleanup_requested = True
        self._closed = False
        self._retry_at = 0.0
        self._failures = 0
        self._last_error = ""
        self._last_tick_ms = 0.0
        self._wake_at = None
        self._deletion_retry_at = 0.0
        with self.repo.transaction():
            self.repo.conn.execute("CREATE TABLE IF NOT EXISTS retrieval_vector_failures ("
                "key_id TEXT PRIMARY KEY, generation TEXT NOT NULL, source_hash TEXT NOT NULL, "
                "search_text TEXT NOT NULL, attempts INTEGER NOT NULL, next_attempt REAL NOT NULL, "
                "quarantined INTEGER NOT NULL, error_type TEXT NOT NULL)")
            self.repo.conn.execute("CREATE INDEX IF NOT EXISTS vector_failure_retry "
                                   "ON retrieval_vector_failures(generation,next_attempt)")

    def _failure_status(self):
        row = self.repo.conn.execute("SELECT COUNT(*),COALESCE(SUM(f.quarantined),0),MIN(f.next_attempt) "
            "FROM retrieval_vector_failures f JOIN retrieval_keys k ON k.id=f.key_id "
            "AND k.source_hash=f.source_hash AND k.search_text=f.search_text WHERE f.generation=? "
            "AND k.indexed_at IS NULL AND COALESCE(json_extract(k.meta,'$.retrieval_status'),'hot')!='cold'",
            (self.index.generation,)).fetchone()
        self._wake_at = row[2]
        return {"retrying_keys": row[0] - row[1], "quarantined_keys": row[1], "next_retry_at": row[2]}

    def request(self):
        self._requested = True

    def status(self):
        errors = self._failure_status()
        return {**self.index.status(), **errors, "degraded": bool(errors["quarantined_keys"]),
                "pending_keys": self.keys.vector_pending_count(),
                "queued": self._requested or self._future is not None,
                "running": self._future is not None, "failures": self._failures,
                "last_error": self._last_error, "tick_ms": round(self._last_tick_ms, 2),
                "time_budget_ms": self.time_budget_ms}

    def rebuild(self):
        # In-flight work keeps its captured generation and cannot acknowledge the new one.
        self.index.reset()
        self.index.prepare(self.repo)
        self.request()
        return {"queued": True, "total": self.index.count(), "vector_status": self.status()}

    def _work(self, generation, batch, deleted):
        result = {"ids": [], "failures": [], "deleted": [], "deletion_error": None}
        try:
            self.index.delete_keys(deleted, generation=generation)
            result["deleted"] = deleted
        except Exception as exc:
            result["deletion_error"] = type(exc).__name__

        def write(part):
            try:
                result["ids"].extend(self.index.upsert_keys(part, generation=generation))
            except Exception as exc:
                transient = isinstance(exc, (TimeoutError, ConnectionError, OSError))
                if len(part) > 1 and not transient:
                    middle = len(part) // 2
                    write(part[:middle])
                    write(part[middle:])
                else:
                    permanent = isinstance(exc, (ValueError, TypeError, UnicodeError))
                    result["failures"].extend((key["id"], type(exc).__name__, permanent) for key in part)
        write(batch)
        return result

    def _acknowledge(self):
        future, snapshot = self._future, self._snapshot
        self._future = self._snapshot = None
        generation, batch, deleted = snapshot
        try:
            result = future.result()
            if isinstance(result, list):  # Obsolete-collection cleanup has no key acknowledgements.
                return
            ids = set(result["ids"])
        except Exception as exc:
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"[:500]
            self._retry_at = time.monotonic() + min(60, 2 ** min(self._failures, 6))
            log.exception("后台向量批次失败，全文检索继续可用")
            return
        self._failures = len(result["failures"])
        self._last_error = (result["failures"][0][1] if result["failures"] else result["deletion_error"]) or ""
        if result["deletion_error"]:
            self._deletion_retry_at = time.monotonic() + 5
        if generation != self.index.generation:
            return
        # A source may change while the worker runs. Never mark its replacement indexed.
        live = self.keys.get_keys([key["id"] for key in batch], list({key["scope"] for key in batch}))
        confirmed = [key["id"] for key in batch if key["id"] in ids and
                     (current := live.get(key["id"])) and
                     all(current.get(field) == key.get(field) for field in
                         ("source_hash", "search_text", "scope", "meta"))]
        with self.repo.transaction():
            self.index.supersede_build_members(self.repo, [key for key in batch
                if key["id"] not in live or any(live[key["id"]].get(field) != key.get(field)
                                                for field in ("source_hash", "search_text", "scope", "meta"))])
            self.keys.mark_indexed(confirmed)
            self.repo.conn.executemany("DELETE FROM retrieval_vector_deletions WHERE id=?",
                                       [(key,) for key in [*result["deleted"], *confirmed]])
            self.repo.conn.executemany("DELETE FROM retrieval_vector_failures WHERE key_id=?", [(key,) for key in confirmed])
            snapshots = {key["id"]: key for key in batch}
            for key_id, error_type, permanent in result["failures"]:
                key, current = snapshots[key_id], live.get(key_id)
                if not current or any(current.get(field) != key.get(field) for field in ("source_hash", "search_text", "scope", "meta")):
                    continue
                previous = self.repo.conn.execute("SELECT attempts FROM retrieval_vector_failures "
                    "WHERE key_id=? AND generation=? AND source_hash=? AND search_text=?",
                    (key_id, generation, key["source_hash"], key["search_text"])).fetchone()
                attempts = (previous[0] if previous else 0) + 1
                quarantined = permanent or attempts >= 5
                next_attempt = self.repo.clock() + (3600 if quarantined else min(60, 2 ** attempts))
                self.repo.conn.execute("INSERT OR REPLACE INTO retrieval_vector_failures VALUES(?,?,?,?,?,?,?,?)",
                    (key_id, generation, key["source_hash"], key["search_text"], attempts, next_attempt,
                     int(quarantined), error_type))
            # A deletion that raced an upsert must be retried, including deleted source rows.
            self.repo.conn.executemany("INSERT OR IGNORE INTO retrieval_vector_deletions VALUES(?)",
                                       [(key,) for key in ids if key not in live])
            self.repo.conn.execute("DELETE FROM retrieval_vector_failures WHERE generation!=? "
                                   "OR key_id NOT IN (SELECT id FROM retrieval_keys)", (generation,))
        self._failure_status()

    def _eligible_batch(self):
        watermark = self.index.seal_build_watermark(self.repo)
        cutoff = watermark.get("key_rowid")
        # Frozen build members come first; later ingestion cannot postpone cutover.
        building = self.index.status()["building"]
        ordering = ("CASE WHEN EXISTS (SELECT 1 FROM retrieval_vector_build_members b WHERE "
                    "b.generation=? AND b.key_id=k.id AND b.superseded=0 AND b.source_hash=k.source_hash "
                    "AND b.search_hash=k.search_hash) THEN 0 ELSE 1 END,") if building and cutoff is not None else ""
        params = [self.index.generation, self.repo.clock()]
        if ordering:
            params.append(self.index.generation)
        params.append(self.batch_size)
        rows = self.repo.conn.execute("SELECT k.id,k.scope,k.source_hash,k.search_text FROM retrieval_keys k WHERE k.indexed_at IS NULL "
            "AND COALESCE(json_extract(k.meta,'$.retrieval_status'),'hot')!='cold' AND NOT EXISTS "
            "(SELECT 1 FROM retrieval_vector_failures f WHERE f.key_id=k.id AND f.generation=? "
            "AND f.source_hash=k.source_hash AND f.search_text=k.search_text AND f.next_attempt>?) "
            "ORDER BY " + ordering + "k.id LIMIT ?", params).fetchall()
        live = self.keys.get_keys([row["id"] for row in rows], list({row["scope"] for row in rows}))
        with self.repo.transaction():
            self.index.supersede_build_members(self.repo, [dict(row) for row in rows if row["id"] not in live])
        return list(live.values())

    def tick(self):
        if self._closed:
            return
        started = time.monotonic()
        deadline = started + self.time_budget_ms / 1000
        try:
            if self._future is not None:
                if not self._future.done():
                    return
                self._acknowledge()
            if time.monotonic() < self._retry_at or time.monotonic() >= deadline:
                return
            dirty = self.repo.conn.execute("SELECT 1 FROM retrieval_dirty_sources LIMIT 1").fetchone()
            if dirty:
                self.keys.sync(repair_fts=False, limit_sources=8)
                self._requested = True
            if self._wake_at is not None and self.repo.clock() >= self._wake_at:
                self._requested = True
            if self._deletion_retry_at and time.monotonic() >= self._deletion_retry_at:
                self._deletion_retry_at = 0
                self._requested = True
            if not self._requested:
                return
            self.index.prepare(self.repo)
            if time.monotonic() >= deadline:
                return
            # Promote the frozen snapshot even while new eligible deltas keep arriving.
            published = self.index.publish_if_ready(self.repo, cleanup=False, allow_quarantined=True)
            if published:
                generation = self.index.generation
                self._snapshot = generation, [], []
                self._future = self._executor.submit(self.index.retire_obsolete, generation)
                self._cleanup_requested = False
                return
            batch = self._eligible_batch()
            deleted = [row[0] for row in self.repo.conn.execute(
                "SELECT id FROM retrieval_vector_deletions ORDER BY id LIMIT ?", (self.batch_size,))] if not self._deletion_retry_at else []
            if batch or deleted:
                generation = self.index.generation
                self._snapshot = generation, batch, deleted
                self._future = self._executor.submit(self._work, generation, batch, deleted)
            else:
                published = self.index.publish_if_ready(self.repo, cleanup=False, allow_quarantined=True)
                if published or (self._cleanup_requested and not self.index.status()["building"]):
                    generation = self.index.generation
                    self._snapshot = generation, [], []
                    self._future = self._executor.submit(self.index.retire_obsolete, generation)
                    self._cleanup_requested = False
                self._requested = bool(self.repo.conn.execute(
                    "SELECT 1 FROM retrieval_dirty_sources LIMIT 1").fetchone())
                self._failure_status()
        except Exception:
            log.exception("向量维护调度失败")
            self._retry_at = time.monotonic() + 5
        finally:
            self._last_tick_ms = (time.monotonic() - started) * 1000

    def close(self):
        self._closed = True
        # Worker never touches SQLite, so shutdown need not hold the RPC connection open.
        self._executor.shutdown(wait=False, cancel_futures=True)
