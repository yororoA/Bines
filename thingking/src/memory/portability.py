"""Portable, verified SQLite snapshots without models or runtime credentials.

Bundles contain the complete memory database, including its job ledger. They are
not full-application backups: gateway delivery queues, autonomy Redis, persona,
credentials, and derived vector indexes are deliberately outside this format.
Import never merges or replaces a database and does not start any services.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import time
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .repository import SCHEMA_VERSION

FORMAT = "bines-memory-bundle"
VERSION = 1
MAX_DATABASE_BYTES = 16 * 1024 ** 3
MAX_MANIFEST_BYTES = 1024 ** 2
CHUNK_BYTES = 1024 ** 2
VALIDATION_TIMEOUT_SECONDS = 30
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "memory.db"
EXCLUDED = ["vector_indexes", "persona", "credentials", "gateway_database_and_delivery_queues",
            "autonomy_redis", "media", "models", "application_configuration", "other_databases"]
WARNINGS = [
    "这是记忆数据库迁移包，不是整个 Bines 应用备份；人格、网关、自主状态、配置及凭据需单独迁移。",
    "未携带向量索引；目标服务启动后按正常维护流程重建，或运行 rebuild-index。",
    "记忆整理任务及租约原样保留，启动服务后按现有任务恢复机制继续；包内不含网关发送队列。",
]
_REQUIRED_COLUMNS = {
    "meta": {"key", "value"},
    "messages": {"id", "conversation_id", "scope", "content"},
    "memories": {"id", "kind", "scope", "content", "deleted"},
    "conversations": {"id", "channel", "scope"},
    "state": {"scope", "data", "version"},
    "jobs": {"id", "kind", "status", "payload", "lease_until"},
}


class BundleError(ValueError):
    """An invalid bundle or a destination that cannot be safely published."""


def _readonly(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _database_details(path: Path) -> dict:
    deadline = time.monotonic() + VALIDATION_TIMEOUT_SECONDS

    def check_deadline():
        if time.monotonic() >= deadline:
            raise BundleError("SQLite 完整性校验超时；请检查数据库大小与状态")

    try:
        with _readonly(path) as connection:
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            connection.execute("BEGIN")
            for table, columns in _REQUIRED_COLUMNS.items():
                check_deadline()
                actual = {row[1] for row in connection.execute(f"PRAGMA table_info({_quote(table)})")}
                if not columns.issubset(actual):
                    raise BundleError(f"不是受支持的 Bines 记忆数据库：缺少 {table} 必需字段")
            row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            try:
                version = int(row[0]) if row else 0
            except (TypeError, ValueError):
                version = 0
            if version < 1 or version > SCHEMA_VERSION:
                raise BundleError(f"不支持数据库版本 {version}；当前最高支持 {SCHEMA_VERSION}")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BundleError("SQLite 完整性校验失败")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise BundleError("SQLite 外键完整性校验失败")
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            if len(tables) > 4096:
                raise BundleError("数据库表数量超过支持范围")
            counts = {}
            for name in tables:
                check_deadline()
                counts[name] = connection.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]
            check_deadline()
            return {"schema_version": version, "tables": counts}
    except sqlite3.Error as exc:
        if time.monotonic() >= deadline:
            raise BundleError("SQLite 完整性校验超时；请检查数据库大小与状态") from exc
        raise BundleError("无法校验 SQLite 记忆数据库") from exc
    finally:
        if "connection" in locals():
            connection.close()


def _summary(manifest: dict) -> dict:
    return {"format": FORMAT, "version": VERSION, "created_at": manifest["created_at"],
            "database": manifest["database"], "included": ["complete_memory_database"],
            "excluded": list(EXCLUDED), "warnings": list(WARNINGS)}


def _unused(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise BundleError("目标已存在；迁移工具不会覆盖已有文件")


def _publish_file(staged: Path, target: Path) -> None:
    # Both files live on the same filesystem. A hard link publishes the complete
    # file atomically and fails if another process created the destination.
    try:
        os.link(staged, target)
    except FileExistsError as exc:
        raise BundleError("目标已存在；迁移工具不会覆盖已有文件") from exc


def export_bundle(db_path, output_path, *, timeout_seconds=30) -> dict:
    """Online SQLite backup (including committed WAL), then atomic ZIP publication."""
    source, output = Path(db_path).expanduser(), Path(output_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError("找不到源记忆数据库")
    _unused(output)
    if output.resolve() in {Path(str(source.resolve()) + suffix)
                           for suffix in ("", "-wal", "-shm", "-journal")}:
        raise BundleError("导出目标不能是源数据库或其 SQLite sidecar")
    if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise BundleError("备份超时必须为有限正数")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bines-memory-export-", dir=output.parent) as directory:
        temporary = Path(directory)
        snapshot = temporary / DATABASE_NAME
        deadline = time.monotonic() + timeout_seconds

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise BundleError("SQLite 快照超时；原数据库保持不变，请稍后重试")
            if total * page_size > MAX_DATABASE_BYTES:
                raise BundleError("数据库超过 16 GiB 迁移包限制")

        incoming = _readonly(source)
        destination = sqlite3.connect(snapshot)
        snapshot.chmod(0o600)
        try:
            page_size = incoming.execute("PRAGMA page_size").fetchone()[0]
            incoming.backup(destination, pages=256, progress=progress, sleep=0.05)
            # A snapshot is self-contained: never ship WAL/SHM or a hot journal.
            destination.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error as exc:
            raise BundleError("SQLite 快照失败；原数据库保持不变") from exc
        finally:
            destination.close()
            incoming.close()
        size = snapshot.stat().st_size
        if size > MAX_DATABASE_BYTES:
            raise BundleError("数据库超过 16 GiB 迁移包限制")
        details = _database_details(snapshot)
        manifest = {"format": FORMAT, "version": VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "database": {"name": DATABASE_NAME, "bytes": size, "sha256": _digest(snapshot), **details},
                    "excluded": list(EXCLUDED), "jobs": "preserved_with_existing_lease_recovery"}
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise BundleError("数据库清单超过大小限制")
        archive = temporary / "bundle.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
            bundle.writestr(MANIFEST_NAME, encoded)
            bundle.write(snapshot, DATABASE_NAME)
        archive.chmod(0o600)
        _publish_file(archive, output)
    return {**_summary(manifest), "path": str(output.resolve())}


def _extract_verified(archive: Path, directory: Path) -> dict:
    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            entries = bundle.infolist()
            if len(entries) != 2 or {entry.filename for entry in entries} != {MANIFEST_NAME, DATABASE_NAME}:
                raise BundleError("包必须且只能包含 manifest.json 和 memory.db，不能包含重复项或路径")
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.is_dir() or (stat.S_IFMT(mode) and not stat.S_ISREG(mode))
                        or entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise BundleError("包中包含不支持的文件类型或压缩方式")
                limit = MAX_MANIFEST_BYTES if entry.filename == MANIFEST_NAME else MAX_DATABASE_BYTES
                if entry.file_size < 0 or entry.file_size > limit:
                    raise BundleError("迁移包解压大小超过限制")
            raw = bundle.read(MANIFEST_NAME)
            manifest = json.loads(raw.decode("utf-8"))
            _validate_manifest(manifest)
            expected = manifest["database"]
            if bundle.getinfo(DATABASE_NAME).file_size != expected["bytes"]:
                raise BundleError("数据库字节数与清单不符")
            database = directory / DATABASE_NAME
            digest, length = hashlib.sha256(), 0
            with bundle.open(DATABASE_NAME) as incoming, database.open("xb") as outgoing:
                database.chmod(0o600)
                while block := incoming.read(CHUNK_BYTES):
                    length += len(block)
                    if length > expected["bytes"]:
                        raise BundleError("数据库解压大小超过清单限制")
                    digest.update(block)
                    outgoing.write(block)
            if length != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
                raise BundleError("数据库 SHA256 或字节数校验失败")
            details = _database_details(database)
            if details != {key: expected[key] for key in ("schema_version", "tables")}:
                raise BundleError("数据库版本或表计数与清单不符")
            return manifest
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, EOFError, zlib.error, RuntimeError) as exc:
        raise BundleError("无效或损坏的记忆迁移包") from exc


def _validate_manifest(manifest) -> None:
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or type(manifest.get("version")) is not int or manifest.get("version") != VERSION):
        raise BundleError("不支持的记忆迁移包格式或版本")
    database = manifest.get("database")
    if (not isinstance(database, dict) or database.get("name") != DATABASE_NAME
            or not isinstance(manifest.get("created_at"), str)):
        raise BundleError("迁移包清单缺少必要字段")
    size, version, digest, counts = (database.get(name) for name in ("bytes", "schema_version", "sha256", "tables"))
    if type(size) is not int or not 0 < size <= MAX_DATABASE_BYTES:
        raise BundleError("无效的数据库字节数")
    if type(version) is not int or not 1 <= version <= SCHEMA_VERSION:
        raise BundleError("迁移包数据库版本高于当前支持范围或无效")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise BundleError("无效的数据库 SHA256")
    if (not isinstance(counts, dict) or not counts or len(counts) > 4096
            or any(not isinstance(name, str) or type(count) is not int or count < 0 for name, count in counts.items())):
        raise BundleError("无效的数据库表计数")


def inspect_bundle(archive_path) -> dict:
    """Verify every byte, schema and table count without touching live data."""
    with tempfile.TemporaryDirectory(prefix="bines-memory-inspect-") as directory:
        manifest = _extract_verified(Path(archive_path).expanduser(), Path(directory))
    return _summary(manifest)


def _empty_target(target: Path, *, staging: Path | None = None) -> None:
    if (target.is_symlink()
            or (target.exists() and (not target.is_dir()
                                    or any(entry != staging for entry in target.iterdir())))):
        raise BundleError("导入目标必须是不存在或空的目录；不会覆盖、合并现有记忆")


def import_bundle(archive_path, target_dir) -> dict:
    """Validate in isolation, then publish memory.db exclusively to an empty target."""
    target = Path(target_dir).expanduser().absolute()
    _empty_target(target)
    # An existing empty directory may itself be a bind mount or drive root.
    # Stage inside it so atomic hard-link publication stays on its filesystem.
    # For a new target, keep validation in the parent: bad input must not create
    # the destination directory at all.
    staging_inside = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bines-memory-import-",
                                     dir=target if staging_inside else target.parent) as directory:
        temporary = Path(directory)
        own_staging = temporary if staging_inside else None
        manifest = _extract_verified(Path(archive_path).expanduser(), temporary)
        _empty_target(target, staging=own_staging)
        created = False
        try:
            try:
                target.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                _empty_target(target, staging=own_staging)
            _publish_file(temporary / DATABASE_NAME, target / DATABASE_NAME)
        except BaseException:
            if created:
                try:
                    target.rmdir()
                except OSError:
                    pass
            raise
    return {**_summary(manifest), "target_dir": str(target.resolve()),
            "database_path": str((target / DATABASE_NAME).resolve())}
