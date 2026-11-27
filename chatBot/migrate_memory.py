"""Idempotent legacy JSON import; source files are preserved byte-for-byte."""
import argparse
import hashlib
import json
from pathlib import Path
from chatBot.messages import LOCAL_IDENTITY
from chatBot.store import MemoryStore
from chatBot.config import ROOT


def migrate(source, store=None):
    source = Path(source)
    store = store or MemoryStore()
    count = 0
    short = source / "memory_short.json"
    if short.exists():
        data = json.loads(short.read_text(encoding="utf-8-sig"))
        rows = data if isinstance(data, list) else data.get("messages", [])
        with store.connection() as db:
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"}:
                    continue
                content = str(row.get("content") or "")
                key = "legacy:"+hashlib.sha256((str(short.resolve())+str(index)+content).encode()).hexdigest()
                count += db.execute("""INSERT OR IGNORE INTO messages
                    (scope,conversation,actor,name,role,content,raw,external_id,ts)
                    VALUES('owner','local','owner','主人',?,?,?,?,?)""",
                    (row["role"], content, json.dumps(row, ensure_ascii=False), key,
                     float(row.get("timestamp") or short.stat().st_mtime)+index/10000)).rowcount
    dynamic = source / "memory_dynamic.json"
    if dynamic.exists():
        value = json.loads(dynamic.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("memory_dynamic.json 必须为对象")
        with store.connection() as db:
            count += db.execute("INSERT OR IGNORE INTO state VALUES('owner',?)",
                                (json.dumps(value, ensure_ascii=False),)).rowcount
    permanent = source / "memory_permanent.json"
    if permanent.exists():
        content = permanent.read_text(encoding="utf-8-sig")
        json.loads(content)
        with store.connection() as db:
            count += db.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?,?,?,?)",
                                ("legacy:permanent", "owner", "profile", content,
                                 json.dumps({"origin": "legacy"}), permanent.stat().st_mtime)).rowcount
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导入新版 MemoryService，保留源文件并使用轻量词面索引")
    parser.add_argument("--source", type=Path, default=ROOT / "thingking" / "src")
    options = parser.parse_args()
    from thingking.memory_admin import cmd_migrate
    from types import SimpleNamespace
    raise SystemExit(cmd_migrate(SimpleNamespace(
        source=options.source, keep_originals=True, lexical=True,
    )))
