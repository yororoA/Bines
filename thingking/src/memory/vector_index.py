"""Versioned, single key-vector index derived from SQLite; FTS survives every rebuild."""
from __future__ import annotations

import hashlib
import json
import math
import logging
import os
import threading
import uuid
import time
import sqlite3
import struct
from collections import OrderedDict
from pathlib import Path

from .embedding import Embedder, embedding_identity
from .embedding_cache import EmbeddingCache
from .chunking import EmbeddingChunks

COLLECTION = "memories"
PREFIX = "memory_keys_v3_"
log = logging.getLogger("bines.memory.vector_index")


class VectorMatches(list):
    def __init__(self, pairs=(), source_hashes=None, search_hashes=None):
        super().__init__(pairs)
        self.source_hashes = source_hashes or {}
        self.search_hashes = search_hashes


class VectorIndex:
    stores_memory_vectors = False

    def __init__(self, index_dir: Path | str, embedder: Embedder):
        import chromadb

        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.identity = embedding_identity(embedder)
        self._chunks = EmbeddingChunks(embedder)
        self.chunking_signature = self._chunks.signature
        self.identity["chunking"] = self.chunking_signature
        self.embedding_fingerprint = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.hnsw = {"space": "cosine",
                     "ef_construction": max(16, min(2000, int(os.getenv("BINES_MEMORY_HNSW_EF_CONSTRUCTION", "100")))),
                     "ef_search": max(16, min(2000, int(os.getenv("BINES_MEMORY_HNSW_EF_SEARCH", "100")))),
                     "max_neighbors": max(4, min(128, int(os.getenv("BINES_MEMORY_HNSW_MAX_NEIGHBORS", "16"))))}
        self.identity["index_layout"] = {key: value for key, value in self.hnsw.items() if key != "ef_search"}
        self.fingerprint = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self._embedding_lock = threading.Lock()
        self._priority = threading.Condition()
        self._query_waiters = 0
        self._query_wait_seconds = max(0, min(1000, int(os.getenv("BINES_MEMORY_VECTOR_QUERY_WAIT_MS", "100")))) / 1000
        self._microbatch = max(1, min(16, int(os.getenv("BINES_MEMORY_EMBEDDING_MICROBATCH", "4"))))
        self._state_lock = threading.RLock()
        self._query_cache = OrderedDict()
        self.last_query_trace = {}
        self._normalize_newlines = "newlines-to-spaces" in str(self.identity.get("preprocessing", ""))
        self._cache, self._cache_error = None, ""
        try:
            self._cache = EmbeddingCache(self.index_dir / "embedding-cache.sqlite3", self.embedding_fingerprint,
                                        self.identity["dimensions"], normalize_newlines=self._normalize_newlines)
        except (OSError, sqlite3.Error) as exc:
            self._cache_error = type(exc).__name__
        self._client = chromadb.PersistentClient(path=str(self.index_dir))
        self._manifest_path = self.index_dir / "vector-state.json"
        try:
            self._manifest = json.loads(self._manifest_path.read_text())
        except (OSError, ValueError):
            self._manifest = {}
        if not isinstance(self._manifest, dict):
            self._manifest = {}
        active = self._manifest.get("active", {})
        pending = self._manifest.get("pending", {})
        active = active if isinstance(active, dict) else {}
        pending = pending if isinstance(pending, dict) else {}
        self._active = None
        if active.get("fingerprint") == self.fingerprint:
            try:
                self._active = self._client.get_collection(active["collection"], embedding_function=None)
                if (self._active.metadata or {}).get("fingerprint") != self.fingerprint:
                    self._active = None
            except Exception:
                pass
        if pending.get("fingerprint") == self.fingerprint:
            try:
                self._keys = self._client.get_collection(pending["collection"], embedding_function=None)
                if (self._keys.metadata or {}).get("fingerprint") != self.fingerprint:
                    raise ValueError("pending vector identity mismatch")
            except Exception:
                # A lost staging collection must also invalidate persisted SQL marks.
                self.reset()
        elif self._active is not None:
            self._keys = self._active
        else:
            self.reset()
        self._keys.modify(configuration={"hnsw": {"ef_search": self.hnsw["ef_search"]}})

    def split_text(self, text):
        return self._chunks.split(text)

    def fit_text(self, text):
        return self._chunks.fit(text)

    def _text_hash(self, text):
        return hashlib.sha256((text.replace("\n", " ") if self._normalize_newlines else text).encode()).hexdigest()

    def _cache_call(self, operation, value):
        if self._cache is None:
            return {}
        try:
            return getattr(self._cache, operation)(value)
        except (OSError, sqlite3.Error, struct.error, OverflowError) as exc:
            # Cache corruption must never quarantine otherwise valid source keys.
            self._cache_error, self._cache = type(exc).__name__, None
            log.warning("embedding缓存不可用，改为直接计算 (%s)", self._cache_error)
            return {}

    def _open(self, name):
        return self._client.get_or_create_collection(name, embedding_function=None,
            metadata={"fingerprint": self.fingerprint, "identity": json.dumps(self.identity, sort_keys=True)},
            configuration={"hnsw": self.hnsw})

    def _save_manifest(self, manifest=None):
        temporary = self._manifest_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w") as stream:
                json.dump(self._manifest if manifest is None else manifest, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def generation(self):
        return self._keys.name

    def status(self):
        return {"fingerprint": self.fingerprint, "generation": self.generation,
                "building": bool(self._manifest.get("pending")),
                "ready": self._active is not None, "kind": "retrieval_keys", "hnsw": self.hnsw,
                "embedding_cache_available": self._cache is not None, "embedding_cache_error": self._cache_error,
                "query_wait_ms": self._query_wait_seconds * 1000, "embedding_microbatch": self._microbatch}

    def prepare(self, repo):
        repo.conn.execute("""CREATE TABLE IF NOT EXISTS retrieval_vector_build_members (
            generation TEXT NOT NULL, key_id TEXT NOT NULL, source_hash TEXT NOT NULL,
            search_hash TEXT NOT NULL, superseded INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(generation,key_id))""")
        row = repo.conn.execute("SELECT value FROM meta WHERE key='vector_generation'").fetchone()
        if row and row[0] == self.generation:
            return False
        with repo.transaction():
            repo.conn.execute("DELETE FROM retrieval_vector_build_members WHERE generation!=?", (self.generation,))
            repo.conn.execute("UPDATE retrieval_keys SET indexed_at=NULL")
            repo.conn.execute("INSERT INTO meta(key,value) VALUES('vector_generation',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.generation,))
            cutoff = repo.conn.execute("SELECT COALESCE(MAX(generation),0) FROM retrieval_dirty_sources").fetchone()[0]
            repo.conn.execute("INSERT INTO meta(key,value) VALUES('vector_build_watermark',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps({
                    "generation": self.generation, "dirty_generation": cutoff, "key_rowid": None}),))
        return True

    def build_watermark(self, repo):
        row = repo.conn.execute("SELECT value FROM meta WHERE key='vector_build_watermark'").fetchone()
        value = json.loads(row[0]) if row else {}
        return value if value.get("generation") == self.generation else {}

    def seal_build_watermark(self, repo):
        value = self.build_watermark(repo)
        if not value or value.get("key_rowid") is not None:
            return value
        if repo.conn.execute("SELECT 1 FROM retrieval_dirty_sources WHERE generation<=? LIMIT 1",
                             (value["dirty_generation"],)).fetchone():
            return value
        value["key_rowid"] = repo.conn.execute("SELECT COALESCE(MAX(rowid),0) FROM retrieval_keys").fetchone()[0]
        value["deletion_rowid"] = repo.conn.execute("SELECT COALESCE(MAX(rowid),0) FROM retrieval_vector_deletions").fetchone()[0]
        value["snapshot_cursor"], value["snapshot_complete"] = 0, False
        with repo.transaction():
            repo.conn.execute("UPDATE meta SET value=? WHERE key='vector_build_watermark'", (json.dumps(value),))
        return value

    def _snapshot_build_members(self, repo, limit=128):
        """Copy only identities, in bounded pages; text hashes are maintained by
        EvidenceStore's normal dirty-source batches, never recomputed globally.
        """
        value = self.seal_build_watermark(repo)
        if not value or value.get("key_rowid") is None or value.get("snapshot_complete"):
            return value
        cursor = int(value.get("snapshot_cursor", 0))
        rows = repo.conn.execute("SELECT rowid,id,source_hash,search_hash,"
            "COALESCE(json_extract(meta,'$.retrieval_status'),'hot') AS tier FROM retrieval_keys "
            "WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?", (cursor, value["key_rowid"], limit)).fetchall()
        with repo.transaction():
            repo.conn.executemany("INSERT OR IGNORE INTO retrieval_vector_build_members VALUES(?,?,?,?,0)",
                [(self.generation, row["id"], row["source_hash"], row["search_hash"]) for row in rows
                 if row["tier"] != "cold"])
            value["snapshot_cursor"] = rows[-1]["rowid"] if rows else value["key_rowid"]
            value["snapshot_complete"] = len(rows) < limit or value["snapshot_cursor"] >= value["key_rowid"]
            repo.conn.execute("UPDATE meta SET value=? WHERE key='vector_build_watermark'", (json.dumps(value),))
        return value

    def supersede_build_members(self, repo, keys):
        """A captured version that no longer exists must not hold up cutover."""
        if keys:
            repo.conn.executemany("UPDATE retrieval_vector_build_members SET superseded=1 "
                "WHERE generation=? AND key_id=? AND source_hash=? AND search_hash=?",
                [(self.generation, key["id"], key["source_hash"],
                  hashlib.sha256(key["search_text"].encode()).hexdigest()) for key in keys])

    def publish_if_ready(self, repo, *, cleanup=True, allow_quarantined=False):
        if not self._manifest.get("pending") or repo.conn.in_transaction:
            return False
        watermark = self._snapshot_build_members(repo)
        # Synchronous/offline callers can finish a requested rebuild here.
        # The RPC scheduler uses cleanup=False and advances at most one page.
        while cleanup and watermark and watermark.get("key_rowid") is not None and not watermark.get("snapshot_complete"):
            watermark = self._snapshot_build_members(repo)
        if not watermark or not watermark.get("snapshot_complete"):
            return False
        pending = """SELECT 1 FROM retrieval_keys k INDEXED BY idx_retrieval_keys_pending
            CROSS JOIN retrieval_vector_build_members b
            WHERE k.indexed_at IS NULL AND k.rowid<=? AND b.generation=? AND b.key_id=k.id
              AND b.superseded=0 AND b.source_hash=k.source_hash AND b.search_hash=k.search_hash
              AND COALESCE(json_extract(k.meta,'$.retrieval_status'),'hot')!='cold'"""
        if allow_quarantined:
            pending += (" AND NOT EXISTS (SELECT 1 FROM retrieval_vector_failures f WHERE "
                        "f.key_id=k.id AND f.generation=? AND f.source_hash=k.source_hash "
                        "AND f.search_text=k.search_text AND f.quarantined=1)")
        args = (watermark["key_rowid"], self.generation, self.generation) if allow_quarantined else (watermark["key_rowid"], self.generation)
        if repo.conn.execute(pending + " LIMIT 1", args).fetchone():
            return False
        if repo.conn.execute("SELECT 1 FROM retrieval_vector_deletions WHERE rowid<=? LIMIT 1",
                             (watermark["deletion_rowid"],)).fetchone():
            return False
        with self._state_lock:
            updated = {**self._manifest, "active": self._manifest["pending"]}
            updated.pop("pending")
            self._save_manifest(updated)
            self._manifest = updated
            self._active = self._keys
        if cleanup:
            self.retire_obsolete(self.generation)
        return True

    def retire_obsolete(self, generation):
        # Retire old derived collections only after every current key is indexed.
        for collection in self._client.list_collections():
            name = getattr(collection, "name", str(collection))
            with self._state_lock:
                if generation != self.generation:
                    return []
                keep = {self.generation, self._active.name if self._active is not None else None}
                remove = name not in keep and (name in {COLLECTION, "memory_keys_v2"} or name.startswith(PREFIX))
            if remove:
                # reset always creates a fresh name, so an obsolete collection
                # cannot become active/target after this check. Do slow IO unlocked.
                self._client.delete_collection(name)
        return []

    def count(self):
        return int(self._keys.count())

    def reset(self):
        with self._state_lock:
            target = self._open(PREFIX + self.fingerprint[:12] + "_" + uuid.uuid4().hex[:12])
            updated = {**self._manifest, "pending": {"fingerprint": self.fingerprint, "collection": target.name}}
            self._save_manifest(updated)
            self._keys, self._manifest = target, updated

    def upsert_keys(self, keys, *, generation=None):
        keys = [key for key in keys if (json.loads(key["meta"]) if isinstance(key.get("meta"), str)
                                       else key.get("meta") or {}).get("retrieval_status") != "cold"]
        if not keys:
            return []
        target = self._keys if generation is None else self._client.get_collection(generation, embedding_function=None)
        texts = [key["search_text"] for key in keys]
        cached = self._cache_call("get", texts)
        missing = {self._text_hash(text): text for text in texts if self._text_hash(text) not in cached}
        items = list(missing.items())
        for start in range(0, len(items), self._microbatch):
            part = items[start:start + self._microbatch]
            # Query waiters acquire the model between small document batches.
            while True:
                with self._priority:
                    self._priority.wait_for(lambda: self._query_waiters == 0)
                self._embedding_lock.acquire()
                with self._priority:
                    if self._query_waiters == 0:
                        break
                self._embedding_lock.release()
            try:
                vectors = self.embedder.embed_documents([text for _, text in part])
            finally:
                self._embedding_lock.release()
            if len(vectors) != len(part):
                raise ValueError("embedding result count mismatch")
            added = {}
            for (digest, _), vector in zip(part, vectors):
                if len(vector) != self.identity["dimensions"] or not all(math.isfinite(float(x)) and abs(float(x)) <= 3.4028235e38 for x in vector):
                    raise ValueError("embedding dimension mismatch or non-finite values")
                added[digest] = [float(x) for x in vector]
            self._cache_call("put", added)
            cached.update(added)
        embeddings = [cached[self._text_hash(text)] for text in texts]
        metadata = []
        for key in keys:
            extra = key.get("meta") or {}
            if isinstance(extra, str):
                extra = json.loads(extra)
            metadata.append({"scope": key["scope"], "kind": key["kind"], "source_hash": key["source_hash"],
                             "search_hash": hashlib.sha256(key["search_text"].encode()).hexdigest(),
                             "memory_id": str(key.get("memory_id") or ""),
                             "subject_id": str(extra.get("subject_id") or "")})
        target.upsert(ids=[key["id"] for key in keys], embeddings=embeddings,
                      documents=[key["search_text"] for key in keys], metadatas=metadata)
        return [key["id"] for key in keys]

    def upsert(self, records):
        """Direct-call compatibility writes the same key collection, never a second index."""
        from .evidence import _chunks, _fingerprint
        keys = []
        for record in records:
            for number, chunk in enumerate(_chunks(record.get("content") or "")):
                keys.append({"id": f"m:{record['id']}:{number}", "memory_id": record["id"],
                             "search_text": chunk, "scope": record["scope"], "kind": record["kind"],
                             "source_hash": _fingerprint(record, "memory"), "meta": record})
        self.upsert_keys(keys)
        return [record["id"] for record in records]

    def delete(self, ids):
        if ids:
            self._keys.delete(where={"memory_id": {"$in": list(ids)}})

    def key_ids(self):
        return self._keys.get(include=[])["ids"]

    def delete_keys(self, ids, *, generation=None):
        if ids:
            target = self._keys if generation is None else self._client.get_collection(generation, embedding_function=None)
            target.delete(ids=list(ids))

    def _query(self, text, scopes, kinds, n, subject_id=None):
        started = time.monotonic()
        trace = {"status": "empty", "cache_hit": False, "wait_ms": 0.0}
        self.last_query_trace = trace
        try:
            target = self._active
            if target is None:
                trace["status"] = "not_ready"
                return None
            count = target.count()
            if not text.strip() or not scopes or count == 0:
                return None
            cache_key = (id(self.embedder), hashlib.sha256(text.encode()).digest())
            with self._state_lock:
                embedding = self._query_cache.get(cache_key)
                if embedding is not None:
                    self._query_cache.move_to_end(cache_key)
            trace["cache_hit"] = embedding is not None
            if embedding is None:
                waiting = time.monotonic()
                with self._priority:
                    self._query_waiters += 1
                acquired = False
                try:
                    acquired = self._embedding_lock.acquire(timeout=self._query_wait_seconds)
                    trace["wait_ms"] = round((time.monotonic() - waiting) * 1000, 2)
                    if not acquired:
                        trace["status"] = "busy"
                        return None
                    embedding = self.embedder.embed_query(text)
                    if len(embedding) != self.identity["dimensions"] or not all(
                            math.isfinite(float(x)) and abs(float(x)) <= 3.4028235e38 for x in embedding):
                        raise ValueError("query embedding dimension mismatch or non-finite values")
                    embedding = [float(x) for x in embedding]
                    with self._state_lock:
                        self._query_cache[cache_key] = embedding
                        if len(self._query_cache) > 64:
                            self._query_cache.popitem(last=False)
                finally:
                    if acquired:
                        self._embedding_lock.release()
                    with self._priority:
                        self._query_waiters -= 1
                        self._priority.notify_all()
            clauses = [{"scope": {"$in": list(scopes)}}]
            if kinds:
                clauses.append({"kind": {"$in": list(kinds)}})
            if subject_id:
                clauses.append({"subject_id": subject_id})
            result = target.query(query_embeddings=[embedding],
                where=clauses[0] if len(clauses) == 1 else {"$and": clauses},
                n_results=max(1, min(int(n), count)), include=["distances", "metadatas"])
            trace["status"] = "ok" if result["ids"][0] else "empty"
            trace["returned"] = len(result["ids"][0])
            return result
        except Exception as exc:
            trace.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            trace["duration_ms"] = round((time.monotonic() - started) * 1000, 2)

    def query_keys(self, text, scopes, kinds=None, n=40):
        result = self._query(text, scopes, kinds, n)
        if result is None:
            return VectorMatches()
        return VectorMatches(zip(result["ids"][0], result["distances"][0]),
                             {key: meta["source_hash"] for key, meta in zip(result["ids"][0], result["metadatas"][0])},
                             {key: meta.get("search_hash") for key, meta in zip(result["ids"][0], result["metadatas"][0])})

    def query(self, text, scopes, kinds=None, n=10, subject_id=None):
        """Legacy memory-level lookup, grouped over the single key collection."""
        requested = min(2048, max(n * 4, 64))
        while True:
            result = self._query(text, scopes, kinds, requested, subject_id)
            grouped = {}
            if result:
                for meta, distance in zip(result["metadatas"][0], result["distances"][0]):
                    mid = meta.get("memory_id")
                    if mid and mid not in grouped:
                        grouped[mid] = float(distance)
            if not result or len(grouped) >= n or len(result["ids"][0]) < requested or requested == 2048:
                return list(grouped.items())[:n]
            requested = min(2048, requested * 2)
