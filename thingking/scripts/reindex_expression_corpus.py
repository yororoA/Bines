"""Re-embed a curated expression JSONL corpus into the dedicated Qdrant index.

The Memory Server owns the embedder, so this script sends a lightweight RPC
instead of loading a second model in a separate process.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "thingking" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from common.env import read_env

for key, value in read_env(ROOT / ".env").items():
    os.environ.setdefault(key, value)

import config
from memory.client import MemoryClient


def main():
    parser = argparse.ArgumentParser()
    # Relative to the project root; the same path exists inside the
    # memory-server container at /app/thingking/data/...
    parser.add_argument(
        "--corpus", type=Path,
        default=Path("thingking/data/expression_corpus/expression_corpus_1k.jsonl"))
    parser.add_argument("--scope", default="owner")
    args = parser.parse_args()
    if not args.corpus.is_file():
        raise SystemExit(f"语料文件不存在: {args.corpus}")
    client = MemoryClient(config.ZMQ_HOST, config.ZMQ_PORTS["RAG_SERVER_REQREP"])
    if not client.ping(timeout_ms=2000):
        raise SystemExit("Memory Server 未运行")
    result = client.call("reindex_expression_corpus", path=str(args.corpus), scope=args.scope)
    print(result)
    print(client.call("expression_vector_status"))


if __name__ == "__main__":
    main()
