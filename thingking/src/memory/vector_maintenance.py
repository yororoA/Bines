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

    def request(self):
        self._requested = True

    def status(self):
        return {**self.index.status(), "queued": self._requested or self._future is not None,
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
        self.index.delete_keys(deleted, generation=generation)
        return self.index.upsert_keys(batch, generation=generation)

    def _acknowledge(self):
        future, snapshot = self._future, self._snapshot
        self._future = self._snapshot = None
        generation, batch, deleted = snapshot
        try:
            ids = set(future.result())
        except Exception as exc:
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"[:500]
            self._retry_at = time.monotonic() + min(60, 2 ** min(self._failures, 6))
            log.exception("后台向量批次失败，全文检索继续可用")
            return
        self._failures, self._last_error = 0, ""
        if generation != self.index.generation:
            return
        # A source may change while the worker runs. Never mark its replacement indexed.
        live = self.keys.get_keys([key["id"] for key in batch], list({key["scope"] for key in batch}))
        confirmed = [key["id"] for key in batch if key["id"] in ids and
                     (current := live.get(key["id"])) and
                     all(current.get(field) == key.get(field) for field in
                         ("source_hash", "search_text", "scope", "meta"))]
        with self.repo.transaction():
            self.keys.mark_indexed(confirmed)
            self.repo.conn.executemany("DELETE FROM retrieval_vector_deletions WHERE id=?",
                                       [(key,) for key in [*deleted, *confirmed]])
            # A deletion that raced an upsert must be retried, including deleted source rows.
            self.repo.conn.executemany("INSERT OR IGNORE INTO retrieval_vector_deletions VALUES(?)",
                                       [(key,) for key in ids if key not in live])

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
            if not self._requested:
                return
            self.index.prepare(self.repo)
            if time.monotonic() >= deadline:
                return
            batch = self.keys.unindexed_keys(limit=self.batch_size)
            deleted = [row[0] for row in self.repo.conn.execute(
                "SELECT id FROM retrieval_vector_deletions ORDER BY id LIMIT ?", (self.batch_size,))]
            if batch or deleted:
                generation = self.index.generation
                self._snapshot = generation, batch, deleted
                self._future = self._executor.submit(self._work, generation, batch, deleted)
            else:
                published = self.index.publish_if_ready(self.repo, cleanup=False)
                if published or (self._cleanup_requested and not self.index.status()["building"]):
                    generation = self.index.generation
                    self._snapshot = generation, [], []
                    self._future = self._executor.submit(self.index.retire_obsolete, generation)
                    self._cleanup_requested = False
                self._requested = bool(self.repo.conn.execute(
                    "SELECT 1 FROM retrieval_dirty_sources LIMIT 1").fetchone())
        except Exception:
            log.exception("向量维护调度失败")
            self._retry_at = time.monotonic() + 5
        finally:
            self._last_tick_ms = (time.monotonic() - started) * 1000

    def close(self):
        self._closed = True
        # Worker never touches SQLite, so shutdown need not hold the RPC connection open.
        self._executor.shutdown(wait=False, cancel_futures=True)
