"""Request-local SQLite reads; cached proofs never survive a database change."""
from contextlib import contextmanager
from contextvars import ContextVar


class MemoryReadCache:
    def __init__(self, repo):
        self.conn = repo.conn
        self.current = ContextVar(f"memory_reads_{id(repo)}", default=None)

    def _epoch(self):
        # total_changes covers this connection, data_version covers other writers.
        return self.conn.total_changes, self.conn.execute("PRAGMA data_version").fetchone()[0]

    @contextmanager
    def session(self):
        if self.current.get() is not None:
            yield
            return
        token = self.current.set({"epoch": self._epoch(), "values": {}})
        try:
            yield
        finally:
            self.current.reset(token)

    def get(self, namespace, key, load):
        state = self.current.get()
        if state is None:
            return load()
        epoch = self._epoch()
        if state["epoch"] != epoch:
            state["values"].clear()
            state["epoch"] = epoch
        cache_key = namespace, key
        if cache_key in state["values"]:
            return state["values"][cache_key]
        result = load()
        # A loader may repair derived rows. Cache only against its final epoch.
        epoch = self._epoch()
        if state["epoch"] != epoch:
            state["values"].clear()
            state["epoch"] = epoch
        state["values"][cache_key] = result
        return result


def read_cache(repo):
    cache = getattr(repo, "_memory_read_cache", None)
    if cache is None:
        cache = repo._memory_read_cache = MemoryReadCache(repo)
    return cache
