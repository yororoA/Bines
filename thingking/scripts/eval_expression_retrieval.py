"""Compare lexical and Qdrant semantic recall over paraphrase queries."""
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
from memory.embedding import load_expression_embedder
from memory.expression_learning import _query_score
from memory.expression_vector import ExpressionVectorStore, load_corpus

QUERIES = [
    ("大半夜不睡觉想上天吗", "半夜不睡要出去偷牛吗"),
    ("你夸得也太突然了", "你怎么一点缓冲时间都没有！"),
    ("我哭给你看", "我难受了，我哭了，我要掉小珍珠了"),
    ("你是不是嫌我傻", "那你愿意当我的傻不拉几的吗"),
    ("别上班了陪我", "好好上你的班，不要和我调情"),
    ("他熬夜到三点", "半夜不睡要出去偷牛吗"),
    ("你猜我送你什么", "你猜对了给你一个奖励"),
    ("少陪我一个小时", "少一个小时是不够爱我么"),
]


def lexical_top(corpus, query, limit=3):
    ranked = sorted(corpus, key=lambda item: _query_score(query, item), reverse=True)
    return [item["quote"] for item in ranked[:limit]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path(config.EXPRESSION_CORPUS_PATH))
    parser.add_argument("--collection", default=config.QDRANT_COLLECTION)
    parser.add_argument("--url", default=config.QDRANT_URL)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    corpus = load_corpus(args.corpus)
    store = ExpressionVectorStore(
        args.url, args.collection, load_expression_embedder(),
        timeout=config.QDRANT_TIMEOUT, min_score=config.QDRANT_MIN_SCORE,
        top_k=config.QDRANT_TOP_K)
    lexical_hits = vector_hits = 0
    for query, expected in QUERIES:
        lexical = lexical_top(corpus, query, args.limit)
        vector = [item["sample"] for item in store.search("owner", query, args.limit)]
        lexical_ok = expected in lexical
        vector_ok = expected in vector
        lexical_hits += lexical_ok
        vector_hits += vector_ok
        print(f"\nQ: {query}\n  expected: {expected}")
        print(f"  lexical: {lexical_ok} {lexical}")
        print(f"  vector : {vector_ok} {vector}")
    total = len(QUERIES)
    print(f"\nrecall@{args.limit}: lexical={lexical_hits}/{total} vector={vector_hits}/{total}")


if __name__ == "__main__":
    main()
