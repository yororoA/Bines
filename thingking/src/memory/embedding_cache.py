"""Disposable content-addressed embedding cache; stores hashes and float32 only."""
from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
import time
from contextlib import contextmanager


class EmbeddingCache:
    def __init__(self, path, fingerprint, dimensions, capacity=20000, normalize_newlines=False):
        self.path, self.fingerprint, self.dimensions = str(path), fingerprint, dimensions
        self.capacity = max(1, capacity)
        self.normalize_newlines = normalize_newlines
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS vectors (model TEXT, text_hash TEXT, "
                         "vector BLOB NOT NULL, touched REAL NOT NULL, PRIMARY KEY(model,text_hash))")
            conn.execute("CREATE INDEX IF NOT EXISTS vectors_touched ON vectors(touched)")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=3)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def digest(self, text):
        # This is exactly the preprocessing applied by the configured HF wrapper.
        value = text.replace("\n", " ") if self.normalize_newlines else text
        return hashlib.sha256(value.encode()).hexdigest()

    def get(self, texts):
        found = {}
        with self._connect() as conn:
            for digest in dict.fromkeys(self.digest(text) for text in texts):
                row = conn.execute("SELECT vector FROM vectors WHERE model=? AND text_hash=?",
                                   (self.fingerprint, digest)).fetchone()
                if row and len(row[0]) == self.dimensions * 4:
                    vector = list(struct.unpack(f"<{self.dimensions}f", row[0]))
                    if all(math.isfinite(x) for x in vector):
                        found[digest] = vector
            conn.executemany("UPDATE vectors SET touched=? WHERE model=? AND text_hash=?",
                             [(time.time(), self.fingerprint, key) for key in found])
        return found

    def put(self, entries):
        with self._connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)", [
                (self.fingerprint, digest, struct.pack(f"<{self.dimensions}f", *vector), time.time())
                for digest, vector in entries.items()])
            count = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            if count > self.capacity:
                conn.execute("DELETE FROM vectors WHERE rowid IN (SELECT rowid FROM vectors "
                             "ORDER BY touched LIMIT ?)", (count - self.capacity,))
