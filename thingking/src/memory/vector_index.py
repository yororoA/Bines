"""Versioned, single key-vector index derived from SQLite; FTS survives every rebuild."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

from .embedding import Embedder, embedding_identity

COLLECTION = "memories"
PREFIX = "memory_keys_v3_"


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
        self.fingerprint = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self._embedding_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._query_cache = OrderedDict()
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

    def _open(self, name):
        return self._client.get_or_create_collection(name, embedding_function=None,
            metadata={"hnsw:space": "cosine", "fingerprint": self.fingerprint,
                      "identity": json.dumps(self.identity, sort_keys=True)})

    def _save_manifest(self):
        temporary = self._manifest_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w") as stream:
                json.dump(self._manifest, stream, ensure_ascii=False, sort_keys=True)
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
                "ready": self._active is not None, "kind": "retrieval_keys"}

    def prepare(self, repo):
        row = repo.conn.execute("SELECT value FROM meta WHERE key='vector_generation'").fetchone()
        if row and row[0] == self.generation:
            return False
        with repo.transaction():
            repo.conn.execute("UPDATE retrieval_keys SET indexed_at=NULL")
            repo.conn.execute("INSERT INTO meta(key,value) VALUES('vector_generation',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.generation,))
        return True

    def publish_if_ready(self, repo, *, cleanup=True):
        if not self._manifest.get("pending") or repo.conn.in_transaction:
            return False
        for table, where in (("retrieval_keys", "WHERE indexed_at IS NULL"),
                             ("retrieval_dirty_sources", ""), ("retrieval_vector_deletions", "")):
            if repo.conn.execute(f"SELECT 1 FROM {table} {where} LIMIT 1").fetchone():
                return False
        with self._state_lock:
            self._manifest["active"] = self._manifest.pop("pending")
            self._save_manifest()
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
            self._keys = self._open(PREFIX + self.fingerprint[:12] + "_" + uuid.uuid4().hex[:12])
            self._manifest["pending"] = {"fingerprint": self.fingerprint, "collection": self.generation}
            self._save_manifest()

    def upsert_keys(self, keys, *, generation=None):
        if not keys:
            return []
        target = self._keys if generation is None else self._client.get_collection(generation, embedding_function=None)
        with self._embedding_lock:
            embeddings = self.embedder.embed_documents([key["search_text"] for key in keys])
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
        target = self._active
        if not text.strip() or not scopes or target is None or target.count() == 0:
            return None
        # A maintenance embedding never makes foreground requests wait for its lock.
        if not self._embedding_lock.acquire(blocking=False):
            return None
        try:
            cache_key = (id(self.embedder), hashlib.sha256(text.encode()).digest())
            embedding = self._query_cache.get(cache_key)
            if embedding is None:
                embedding = self.embedder.embed_query(text)
                self._query_cache[cache_key] = embedding
                if len(self._query_cache) > 8:
                    self._query_cache.popitem(last=False)
            else:
                self._query_cache.move_to_end(cache_key)
        finally:
            self._embedding_lock.release()
        clauses = [{"scope": {"$in": list(scopes)}}]
        if kinds:
            clauses.append({"kind": {"$in": list(kinds)}})
        if subject_id:
            clauses.append({"subject_id": subject_id})
        return target.query(query_embeddings=[embedding],
            where=clauses[0] if len(clauses) == 1 else {"$and": clauses},
            n_results=max(1, min(int(n), target.count())), include=["distances", "metadatas"])

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
