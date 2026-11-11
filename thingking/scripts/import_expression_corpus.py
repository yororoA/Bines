"""Import a public expression corpus into the owner's expression notes.

The corpus is a JSONL file with quote/context/technique/situation/source fields.
Each source is recorded as a verified web_fetch experience, then the existing
ExpressionLearning path stores the quote. Re-running is idempotent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "thingking" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from common.env import read_env

for key, value in read_env(ROOT / ".env").items():
    os.environ.setdefault(key, value)

import config
from memory.channels import local_envelope
from memory.client import MemoryClient
from memory.expression_learning import SITUATIONS, TECHNIQUES

DEFAULT_CORPUS = ROOT / "thingking" / "data" / "expression_corpus" / "lccc_samples.jsonl"


def load_corpus(path):
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        item = json.loads(line)
        required = ("quote", "context", "technique", "situation", "source", "source_id")
        missing = [key for key in required if not isinstance(item.get(key), str) or not item[key].strip()]
        if missing:
            raise ValueError(f"{path}:{line_number} 缺少字段 {missing}")
        if item["technique"] not in TECHNIQUES or item["situation"] not in SITUATIONS:
            raise ValueError(f"{path}:{line_number} technique/situation 不在允许枚举内")
        if item["quote"] not in item["context"]:
            raise ValueError(f"{path}:{line_number} quote 不在 context 中")
        records.append(item)
    if not records:
        raise ValueError("语料文件为空")
    return records


def grouped(records):
    groups = {}
    for record in records:
        groups.setdefault(record["source_id"], []).append(record)
    return groups


def source_content(group):
    contexts = []
    for record in group:
        if record["context"] not in contexts:
            contexts.append(record["context"])
    return "\n".join(contexts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--scope", default="owner")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    records = load_corpus(args.corpus)
    groups = grouped(records)
    for source_id, group in groups.items():
        print(f"{source_id}: {len(group)} 条, {len(source_content(group))} 字")
    if args.dry_run:
        return
    client = MemoryClient(config.ZMQ_HOST, config.ZMQ_PORTS["RAG_SERVER_REQREP"])
    if not client.ping(timeout_ms=1500):
        raise SystemExit("Memory Server 未运行；先启动 memory-server 再导入")
    accepted = duplicates = 0
    for source_id, group in groups.items():
        content = source_content(group)
        now = time.time()
        record = {
            "category": "tool",
            "action": "web_fetch",
            "status": "succeeded",
            "args": {"url": group[0]["source"]},
            "result": {"status": "ok", "content": content},
            "started_at": now,
            "finished_at": now,
        }
        client.call("record_experience", envelope=local_envelope("", timestamp=now).to_dict(),
                    source_ref=source_id, record=record, revision=1)
        source = {"kind": "experience", "id": source_id, "revision": 1, "path": ["content"]}
        for item in group:
            result = client.learn_expression(args.scope, source, item["quote"],
                                             item["technique"], item["situation"])
            status = result.get("status")
            accepted += status == "accepted"
            duplicates += status == "duplicate"
            print(f"{status}: {item['quote']} -> {item['technique']}/{item['situation']}")
            time.sleep(0.01)
    print(f"done: accepted={accepted}, duplicate={duplicates}")


if __name__ == "__main__":
    main()
