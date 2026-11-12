"""Durable autonomy documents, revision checks and renewable scheduler leases.

Redis is the shared production store. SQLite is an explicitly selected local
backend, never a fallback after a Redis error. Legacy files/tables are read once.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class AutonomyStoreError(RuntimeError):
    pass


class StateConflict(AutonomyStoreError):
    pass


def memory_directory():
    path = Path(os.getenv("BINES_MEMORY_DIR", str(ROOT / "thingking/memory_data")))
    return path if path.is_absolute() else ROOT / path


def legacy_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise AutonomyStoreError("旧自主状态无法读取，迁移已暂停") from None
    if not isinstance(data, dict):
        raise AutonomyStoreError("旧自主状态格式无效，迁移已暂停")
    return data


def prepare_redis(directory=None):
    """Create stable private credentials and the Compose server configuration."""
    import secrets
    directory = Path(directory or memory_directory())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "autonomy_redis.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"password": secrets.token_hex(32)}, stream)
    password = json.loads(path.read_text())["password"]
    if not isinstance(password, str) or not password.isalnum():
        raise AutonomyStoreError("Redis 凭据文件格式无效")
    config = directory / "autonomy_redis.conf"
    content = (
        "bind 0.0.0.0\nprotected-mode yes\nport 6379\ndir /data\n"
        "appendonly yes\nappendfsync always\nsave 60 1\nmaxmemory-policy noeviction\n"
        f"requirepass {password}\n"
    )
    if not config.exists() or config.read_text() != content:
        # Use a private atomic replacement, without printing credentials.
        temporary = config.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        temporary.replace(config)
    return config


class AutonomyStore:
    _CAS = """
local revision = tonumber(redis.call('HGET', KEYS[1], 'revision') or '0')
if revision ~= tonumber(ARGV[1]) then return -1 end
redis.call('HSET', KEYS[1], 'revision', revision + 1, 'value', ARGV[2])
return revision + 1
"""
    _RENEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('PEXPIRE', KEYS[1], ARGV[2])
"""
    _RELEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""

    def __init__(self, *, directory=None, sqlite_path=None, backend=None, url=None, prefix=None):
        self.directory = Path(directory or memory_directory())
        self.backend = backend or os.getenv("BINES_AUTONOMY_BACKEND", "redis").lower()
        self.prefix = prefix or os.getenv("BINES_AUTONOMY_PREFIX", "bines:autonomy:")
        self._lease = threading.local()
        if self.backend == "redis":
            try:
                import redis
                credentials = self.directory / "autonomy_redis.json"
                password = json.loads(credentials.read_text())["password"] if credentials.exists() else None
                self.redis = redis.Redis.from_url(
                    url or os.getenv("BINES_AUTONOMY_REDIS_URL", "redis://127.0.0.1:6380/0"),
                    password=password, decode_responses=True, socket_timeout=3,
                    socket_connect_timeout=3,
                )
            except Exception as exc:
                raise AutonomyStoreError(f"自主状态 Redis 配置不可用（{type(exc).__name__}）") from None
        elif self.backend == "sqlite":
            self.path = Path(sqlite_path or os.getenv(
                "BINES_AUTONOMY_SQLITE_PATH", str(self.directory / "autonomy.db")))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                db.executescript(
                    "PRAGMA journal_mode=WAL;"
                    "CREATE TABLE IF NOT EXISTS documents "
                    "(key TEXT PRIMARY KEY, revision INTEGER NOT NULL, value TEXT NOT NULL);"
                    "CREATE TABLE IF NOT EXISTS leases "
                    "(key TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);"
                )
        else:
            raise ValueError("BINES_AUTONOMY_BACKEND 必须是 redis 或 sqlite")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _call(self, method, *args, **kwargs):
        try:
            return getattr(self.redis, method)(*args, **kwargs)
        except Exception as exc:
            # Redis exceptions may contain a URL/password; expose only the type.
            raise AutonomyStoreError(f"自主状态 Redis 不可用（{type(exc).__name__}），已暂停自主调度") from None

    def _read(self, key):
        if self.backend == "redis":
            row = self._call("hgetall", self.prefix + key)
            return (json.loads(row["value"]), int(row["revision"])) if row else ({}, 0)
        with self._db() as db:
            row = db.execute("SELECT value,revision FROM documents WHERE key=?", (key,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else ({}, 0)

    def get(self, key, *, seed=None):
        value, revision = self._read(key)
        if revision == 0 and seed is not None:
            value = seed()
            try:
                revision = self.put(key, value, 0)
            except StateConflict:
                return self._read(key)
        return value, revision

    def put(self, key, value, revision):
        self.assert_lease()
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if self.backend == "redis":
            updated = self._call("eval", self._CAS, 1, self.prefix + key, revision, payload)
            if updated == -1:
                raise StateConflict("自主状态已被其他进程更新，需重新加载")
            return updated
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM documents WHERE key=?", (key,)).fetchone()
            current = row[0] if row else 0
            if current != revision:
                raise StateConflict("自主状态已被其他进程更新，需重新加载")
            db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?)", (key, revision + 1, payload))
        return revision + 1

    def update(self, key, transform, *, seed=None):
        for _ in range(12):
            value, revision = self.get(key, seed=seed)
            updated = transform(value)
            try:
                self.put(key, updated, revision)
                return updated
            except StateConflict:
                continue
        raise StateConflict("自主状态更新冲突过多，需稍后重试")

    def _lease_change(self, key, token, seconds, *, acquire=False, release=False):
        if self.backend == "redis":
            key = self.prefix + "lease:" + key
            if acquire:
                return bool(self._call("set", key, token, nx=True, px=int(seconds * 1000)))
            return bool(self._call("eval", self._RELEASE if release else self._RENEW,
                                   1, key, token, int(seconds * 1000)))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if acquire:
                db.execute("DELETE FROM leases WHERE key=? AND expires<=?", (key, time.time()))
                return bool(db.execute("INSERT OR IGNORE INTO leases VALUES(?,?,?)",
                                       (key, token, time.time() + seconds)).rowcount)
            if release:
                return bool(db.execute("DELETE FROM leases WHERE key=? AND token=?", (key, token)).rowcount)
            return bool(db.execute("UPDATE leases SET expires=? WHERE key=? AND token=? AND expires>?",
                                   (time.time() + seconds, key, token, time.time())).rowcount)

    def assert_lease(self):
        lease = getattr(self._lease, "current", None)
        if lease and (lease[3].is_set() or not self._lease_change(*lease[:3])):
            raise StateConflict("自主调度租约失效，已停止本轮")

    @contextmanager
    def scheduler(self, key, *, seconds=30):
        token, stopped, lost = uuid.uuid4().hex, threading.Event(), threading.Event()
        if not self._lease_change(key, token, seconds, acquire=True):
            raise StateConflict("其他调度器正在处理自主状态")
        self._lease.current = (key, token, seconds, lost)

        def renew():
            while not stopped.wait(seconds / 3):
                try:
                    if not self._lease_change(key, token, seconds):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=renew, name="autonomy-lease", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=4)
            self._lease.current = None
            try:
                self._lease_change(key, token, seconds, release=True)
            except AutonomyStoreError:
                pass


if __name__ == "__main__":
    prepare_redis()
    print("自主状态 Redis 配置已准备")
