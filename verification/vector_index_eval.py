"""Offline HNSW-vs-exact benchmark on reproducible synthetic vectors; no model calls.

Run with the thinking environment. This measures index recall/latency only,
not Chinese semantic retrieval, generation quality, or end-to-end RPC latency.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from importlib.metadata import version


def evaluate(size=2000, dimensions=64, queries=64, k=10, ef_values=(32, 100, 256), seed=731,
             construction=100, neighbors=16):
    import chromadb
    import numpy as np

    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(32, dimensions))
    vectors = centers[rng.integers(0, len(centers), size)] + rng.normal(scale=.35, size=(size, dimensions))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    probes = vectors[rng.integers(0, size, queries)] + rng.normal(scale=.04, size=(queries, dimensions))
    probes /= np.linalg.norm(probes, axis=1, keepdims=True)
    truth, exact_ms = [], []
    for probe in probes:
        started = time.perf_counter()
        nearest = np.argsort(-(vectors @ probe))[:k]
        exact_ms.append((time.perf_counter() - started) * 1000)
        truth.append(set(map(str, nearest.tolist())))
    result = {"synthetic": True, "model_calls": 0, "index_only": True, "seed": seed,
              "size": size, "dimensions": dimensions, "queries": queries, "k": k,
              "chromadb": version("chromadb"), "ef_construction": construction,
              "max_neighbors": neighbors, "exact_p95_ms": float(np.percentile(exact_ms, 95)), "runs": []}
    with tempfile.TemporaryDirectory(prefix="bines-ann-eval-") as directory:
        client = chromadb.PersistentClient(path=directory)
        collection = client.create_collection("synthetic_benchmark", embedding_function=None,
            configuration={"hnsw": {"space": "cosine", "ef_construction": construction,
                                     "ef_search": ef_values[0], "max_neighbors": neighbors}})
        started = time.perf_counter()
        for offset in range(0, size, 500):
            collection.add(ids=[str(i) for i in range(offset, min(offset + 500, size))],
                           embeddings=vectors[offset:offset + 500].tolist())
        result["build_seconds"] = time.perf_counter() - started
        for ef in ef_values:
            collection.modify(configuration={"hnsw": {"ef_search": ef}})
            for probe in probes[:3]:
                collection.query(query_embeddings=[probe.tolist()], n_results=k, include=[])
            timings, recalls = [], []
            for probe, expected in zip(probes, truth):
                started = time.perf_counter()
                actual = collection.query(query_embeddings=[probe.tolist()], n_results=k, include=[])["ids"][0]
                timings.append((time.perf_counter() - started) * 1000)
                recalls.append(len(set(actual) & expected) / k)
            result["runs"].append({"ef_search": ef, "recall_at_k": float(np.mean(recalls)),
                                   "p50_ms": float(np.percentile(timings, 50)),
                                   "p95_ms": float(np.percentile(timings, 95))})
        client.delete_collection(collection.name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=2000)
    parser.add_argument("--dimensions", type=int, default=64)
    parser.add_argument("--queries", type=int, default=64)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--ef-search", type=int, nargs="+", default=[32, 100, 256])
    parser.add_argument("--ef-construction", type=int, default=100)
    parser.add_argument("--max-neighbors", type=int, default=16)
    parser.add_argument("--seed", type=int, default=731)
    args = parser.parse_args()
    if not (1 <= args.k <= args.size <= 1000000 and 1 <= args.dimensions <= 2048
            and 1 <= args.queries <= 10000 and all(1 <= value <= 2000 for value in args.ef_search)
            and 4 <= args.max_neighbors <= 128 and 16 <= args.ef_construction <= 2000):
        parser.error("invalid size, dimensions, query count or HNSW parameters")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    print(json.dumps(evaluate(args.size, args.dimensions, args.queries, args.k, tuple(args.ef_search),
                             args.seed, args.ef_construction, args.max_neighbors), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
