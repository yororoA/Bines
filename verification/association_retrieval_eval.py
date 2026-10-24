"""Replay an annotated frozen corpus through graph-disabled/enabled lexical retrieval."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from verification.memory_retrieval_eval import (
    catalog_from, load_dataset, normalize_candidates, query_metrics, sqlite_backup, summarize,
)


def run(database, dataset, valid_subset=False):
    """Source DB is opened read-only; all schema and index writes use temp copies."""
    with tempfile.TemporaryDirectory(prefix="bines-association-eval-") as temporary:
        folder = Path(temporary)
        sqlite_backup(database, folder / "frozen.db")
        conn = sqlite3.connect(folder / "frozen.db")
        catalog = catalog_from(conn)
        skipped = []
        if valid_subset:
            from verification.memory_retrieval_eval import file_digest
            all_queries = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
            queries = []
            for query in all_queries:
                refs = [*query["relevant_sources"], *query.get("forbidden_sources", [])]
                valid = all(ref in catalog and query.get("evidence_quotes", {}).get(ref)
                            and query["evidence_quotes"][ref] in catalog[ref]["content"] for ref in refs)
                if valid:
                    queries.append(query)
                else:
                    skipped.append(query["id"])
            dataset_hash = file_digest(dataset)
        else:
            queries, dataset_hash = load_dataset(dataset, catalog)
        conn.close()
        reports = {}
        for enabled in (False, True):
            target = folder / ("graph" if enabled else "hybrid")
            target.mkdir()
            sqlite_backup(folder / "frozen.db", target / "memory.db")
            settings = MemorySettings(data_dir=target, associations_enabled=enabled,
                                      retention_enabled=False, state_updates=False)
            repo = MemoryRepository(settings.db_path, clock=lambda: 1_792_800_000)
            try:
                service = MemoryService(repo, LexicalIndex(repo), settings, clock=lambda: 1_792_800_000)
                service.retriever.sync()
                rows = []
                for q in queries:
                    latency, ranks = [], []
                    for _ in range(3):
                        start = time.perf_counter()
                        candidates = service.search(q["question"], q["scopes"], k=10)
                        latency.append((time.perf_counter() - start) * 1000)
                        ranked, leaks, unknown = normalize_candidates(candidates, "hybrid", catalog, q["scopes"])
                        ranks.append(ranked)
                    rows.append({**q, "latency_ms": latency, "scope_leaks": leaks,
                                 "unresolved_evidence_count": unknown, "unstable": any(r != ranks[0] for r in ranks),
                                 "metrics": query_metrics(ranked, q["relevant_sources"], len(candidates))})
                reports["graph" if enabled else "hybrid"] = summarize(rows)
            finally:
                repo.close()
        return {"dataset_sha256": dataset_hash, "skipped": skipped, "models_called": False, "vector_used": False,
                "links_generated": 0, "retention_enabled": False, "reports": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--valid-subset", action="store_true",
                        help="explicitly exclude labels whose original source/quote no longer exists")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    result = run(args.database.resolve(), args.dataset.resolve(), args.valid_subset)
    rows = ["# 关联记忆首版：固定语料兼容性回放", "",
            "使用只读 SQLite 备份，在两份临时库上重复检索同一标注问题。未调用模型、未生成语义关联、"
            "未使用真实向量，自动冷却关闭。本回放仅检查首版检索接入的兼容性，不能证明关联网络的效果提升。", "",
            f"标注文件 SHA-256：`{result['dataset_sha256']}`", "",
            f"因来源已删除或原文片段改变而排除的标注问题：{len(result['skipped'])}。"
            "只对剩余问题作成对回放，未补造或替换旧标注。", "",
            "| 指标 | 现有混合词面路径 | 开启关联路径（无已建关系） |", "|---|---:|---:|"]
    before, after = result["reports"]["hybrid"], result["reports"]["graph"]
    for label, key in (("问题数", "queries"), ("跨 scope 泄露问题数", "scope_leak_queries"),
                       ("无法解析的证据数", "unresolved_evidence_count"), ("重复执行不稳定问题数", "unstable_queries")):
        rows.append(f"| {label} | {before[key]} | {after[key]} |")
    for k in ("1", "5", "10"):
        rows.append(f"| Evidence Recall@{k} | {before['at_k'][k]['evidence_recall']:.3f} "
                    f"| {after['at_k'][k]['evidence_recall']:.3f} |")
    for percentile in ("p50", "p95"):
        rows.append(f"| 检索延迟 {percentile} / ms | {before['latency_ms'][percentile]:.1f} "
                    f"| {after['latency_ms'][percentile]:.1f} |")
    rows += ["", "后续启用冷却前，仍需在同一语料副本上完成模型真实建联、回答忠实度与 TopK 巩固的消融评测。",
             "这里的延迟仅为本机固定语料描述值；没有运行真实 QQ 登录或远端服务。", ""]
    args.report.write_text("\n".join(rows), encoding="utf-8")
    print(f"固定语料 {before['queries']} 个问题：跨 scope 泄露 {after['scope_leak_queries']}，"
          f"证据无法解析 {after['unresolved_evidence_count']}。报告：{args.report}")


if __name__ == "__main__":
    main()
