"""Memory maintenance CLI (legacy migration/rebuild require stopped services).

  python thingking/memory_admin.py migrate          # snapshot legacy files, import, index, verify, archive originals
  python thingking/memory_admin.py verify           # re-check ledger hashes and recall of migrated memories
  python thingking/memory_admin.py rebuild-index    # drop the Chroma index and rebuild it from SQLite
  python thingking/memory_admin.py stats
  python thingking/memory_admin.py restore-legacy <backup_dir>   # put the legacy files back (rollback)
  python thingking/memory_admin.py export-bundle <output.zip>   # consistent online memory.db snapshot
  python thingking/memory_admin.py inspect-bundle <bundle.zip>  # verify bytes, schema and table counts
  python thingking/memory_admin.py import-bundle <bundle.zip> --target-dir <empty_dir>
"""
import argparse
import json
import random
import shutil
import sys
from pathlib import Path

THINKING_DIR = Path(__file__).resolve().parent
for path in (THINKING_DIR.parent, THINKING_DIR / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from memory import migration  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from memory.settings import SRC_DIR, load_settings  # noqa: E402


def _server_running() -> bool:
    from config import ZMQ_HOST, ZMQ_PORTS
    from memory.client import MemoryClient

    if MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"]).ping(timeout_ms=1500):
        return True
    from chatBot.memory_hub import GatewayMemoryClient
    return GatewayMemoryClient().ping(timeout_ms=1500)


def _open_service(lexical=False):
    from memory.service import MemoryService

    settings = load_settings()
    from chatBot.store import prepare_gateway_database
    prepare_gateway_database(settings.data_dir)
    repo = MemoryRepository(settings.db_path)
    if lexical:
        from memory.lexical_index import LexicalIndex
        index = LexicalIndex(repo)
    else:
        from memory.embedding import load_embedder
        from memory.vector_index import VectorIndex
        index = VectorIndex(settings.index_dir, load_embedder())
    service = MemoryService(repo, index, settings)
    service.sync_identities()
    return service


def _recall_check(service, sample_size: int = 20) -> dict:
    rows = [m for batch in service.repo.all_active_memories() for m in batch if m["origin"] == "legacy"]
    sample = random.Random(0).sample(rows, min(sample_size, len(rows)))
    misses = []
    for row in sample:
        hits = service.index.query(row["content"], [row["scope"]], [row["kind"]], n=3)
        if row["id"] not in [memory_id for memory_id, _ in hits]:
            misses.append(row["id"])
    return {"sampled": len(sample), "missed": misses}


def cmd_migrate(args) -> int:
    if _server_running():
        print("Memory Server 正在运行：请先在管理页停止 RAG Server / RAG Web / Thinking / Bored Detector 再迁移。")
        return 2
    settings = load_settings()
    source_dir = Path(args.source or SRC_DIR)
    present = [n for n in migration.LEGACY_FILES if (source_dir / n).exists()]
    if (source_dir / migration.LEGACY_CHROMA_DIR).exists():
        present.append(migration.LEGACY_CHROMA_DIR)
    if not present:
        print(f"{source_dir} 下没有旧记忆文件，无需迁移。")
        return 0

    snapshot = migration.snapshot_legacy(source_dir, settings.backup_dir)
    corrupted = migration.verify_snapshot(snapshot)
    if corrupted:
        print(f"快照校验失败，已中止：{corrupted}")
        return 1
    print(f"旧数据快照：{snapshot}")

    service = _open_service(lexical=args.lexical)
    migrator = migration.LegacyMigrator(service.repo, settings.owner_qq_ids, settings.day_start_hour)
    report = migrator.run(snapshot)
    print("导入结果：", json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
    print("索引：", service.reconcile_index())

    verification = migration.verify_migration(service.repo, report.source_counts)
    recall = _recall_check(service)
    print("校验：", json.dumps({**verification, "recall": recall}, ensure_ascii=False, indent=1))
    if verification["problems"] or recall["missed"]:
        print("校验未通过：旧文件保持原样未删除，新库可删除 memory_data/memory.db 后重试。")
        return 1

    if not args.keep_originals:
        for name in present:
            path = source_dir / name
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        print(f"校验通过，已移除 {source_dir} 下的旧文件（只读副本保存在 {snapshot}）。")
    print("统计：", json.dumps(service.repo.stats(), ensure_ascii=False))
    return 0


def cmd_verify(args) -> int:
    if _server_running():
        print("Memory Server 或 QQ 网关正在运行：请先停止记忆服务及其客户端再执行离线校验。")
        return 2
    service = _open_service()
    verification = migration.verify_migration(service.repo, {})
    recall = _recall_check(service, sample_size=50)
    print(json.dumps({**verification, "recall": recall}, ensure_ascii=False, indent=1))
    return 1 if verification["problems"] or recall["missed"] else 0


def cmd_rebuild(args) -> int:
    if _server_running():
        print("Memory Server 正在运行：请在记忆管理页点击「重建向量索引」，或先停止服务。")
        return 2
    print(json.dumps(_open_service().rebuild_index(), ensure_ascii=False))
    return 0


def cmd_stats(args) -> int:
    if _server_running():
        from chatBot.memory_hub import GatewayMemoryClient
        from memory.client import MemoryClient
        from config import ZMQ_HOST, ZMQ_PORTS
        remote = MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"])
        if not remote.ping(timeout_ms=1500):
            remote = GatewayMemoryClient()
        print(json.dumps(remote.stats(), ensure_ascii=False, indent=1))
        return 0
    settings = load_settings()
    repo = MemoryRepository(settings.db_path)
    print(json.dumps(repo.stats(), ensure_ascii=False, indent=1))
    return 0


def cmd_restore(args) -> int:
    backup = Path(args.backup_dir)
    corrupted = migration.verify_snapshot(backup)
    if corrupted:
        print(f"备份校验失败：{corrupted}")
        return 1
    target = Path(args.source or SRC_DIR)
    for name in migration.LEGACY_FILES:
        if (backup / name).exists():
            shutil.copy2(backup / name, target / name)
    if (backup / migration.LEGACY_CHROMA_DIR).exists() and not (target / migration.LEGACY_CHROMA_DIR).exists():
        shutil.copytree(backup / migration.LEGACY_CHROMA_DIR, target / migration.LEGACY_CHROMA_DIR)
    print(f"已把旧记忆文件恢复到 {target}")
    return 0


def cmd_bundle(args) -> int:
    """Portable database operations never open a service or load embedding models."""
    from memory.portability import export_bundle, import_bundle, inspect_bundle

    try:
        if args.command == "export-bundle":
            result = export_bundle(args.db or load_settings().db_path, args.archive,
                                   timeout_seconds=args.timeout)
        elif args.command == "inspect-bundle":
            result = inspect_bundle(args.archive)
        else:
            result = import_bundle(args.archive, args.target_dir)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--source", help="旧文件目录，默认 thingking/src")
    migrate.add_argument("--keep-originals", action="store_true", help="校验通过后不删除原位置的旧文件")
    migrate.add_argument("--lexical", action="store_true", help="使用词面索引导入，不加载嵌入模型")
    migrate.set_defaults(func=cmd_migrate)
    sub.add_parser("verify").set_defaults(func=cmd_verify)
    sub.add_parser("rebuild-index").set_defaults(func=cmd_rebuild)
    sub.add_parser("stats").set_defaults(func=cmd_stats)
    restore = sub.add_parser("restore-legacy")
    restore.add_argument("backup_dir")
    restore.add_argument("--source", help="恢复到的目录，默认 thingking/src")
    restore.set_defaults(func=cmd_restore)
    bundle_export = sub.add_parser("export-bundle", help="在线导出完整记忆数据库，不含配置/索引/其他应用数据")
    bundle_export.add_argument("archive", help="新建的 ZIP 文件，不覆盖已有文件")
    bundle_export.add_argument("--db", help="源 memory.db，默认使用 BINES_MEMORY_DIR")
    bundle_export.add_argument("--timeout", type=float, default=30, help="SQLite 在线快照超时秒数，默认 30")
    bundle_export.set_defaults(func=cmd_bundle)
    bundle_inspect = sub.add_parser("inspect-bundle", help="校验迁移包并查看范围与数据库计数")
    bundle_inspect.add_argument("archive")
    bundle_inspect.set_defaults(func=cmd_bundle)
    bundle_import = sub.add_parser("import-bundle", help="仅导入空目录，不合并、不覆盖、不启动服务")
    bundle_import.add_argument("archive")
    bundle_import.add_argument("--target-dir", required=True, help="不存在或为空的目标记忆目录")
    bundle_import.set_defaults(func=cmd_bundle)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
