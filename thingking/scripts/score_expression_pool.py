"""Score candidate expression lines with DeepSeek and assemble the final 1K corpus."""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "thingking" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("HF_HOME", str(ROOT / "thingking" / "models" / "hf-cache"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from common.env import read_env

for key, value in read_env(ROOT / ".env").items():
    os.environ.setdefault(key, value)

import config
from memory.embedding import load_expression_embedder

CURATED = ROOT / "thingking" / "data" / "expression_corpus" / "xiaoheihe_qingtouyihe.jsonl"
POOL = ROOT / "thingking" / "data" / "expression_corpus" / "expression_pool_3000.jsonl"
SCORED = ROOT / "thingking" / "data" / "expression_corpus" / "expression_pool_scored_3000.jsonl"
OUTPUT = ROOT / "thingking" / "data" / "expression_corpus" / "expression_corpus_1k.jsonl"
TARGET = 1000
BATCH = 20
WORKERS = 4

PROMPT = """你是中文聊天语料质量评审。给每条候选句打分 0-10。
标准：像真人日常聊天；有少女感、俏皮、嘴硬、撒娇或能接梗；完整自然，不尴尬；不冒犯、不说教、不像新闻/广告。
只输出 JSON：{"scores":[{"i":1,"score":8},{"i":2,"score":3}]}。
候选：
"""


def score_batch(batch):
    numbered = "\n".join(f"{index}. {row['quote']}" for index, row in enumerate(batch, 1))
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "temperature": 0,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "你是严格的中文聊天语料质量评审，只输出 JSON。"},
            {"role": "user", "content": PROMPT + numbered},
        ],
    }
    for attempt in range(2):
        try:
            response = requests.post(
                config.DEEPSEEK_VISION_API_URL,
                headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=config.DEEPSEEK_VISION_TIMEOUT)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            scores = {}
            for item in data.get("scores") or []:
                try:
                    index, score = int(item["i"]), float(item["score"])
                except (KeyError, TypeError, ValueError):
                    continue
                scores[index] = max(0.0, min(10.0, score))
            if len(scores) == len(batch):
                return [scores[index] for index in range(1, len(batch) + 1)]
        except Exception as exc:
            print(f"score batch failed ({attempt + 1}): {exc}", flush=True)
    return [5.0 for _ in batch]


def main():
    pool = [json.loads(line) for line in POOL.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    curated = [json.loads(line) for line in CURATED.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    print(f"pool={len(pool)} curated={len(curated)}", flush=True)

    if SCORED.is_file():
        cached = [json.loads(line) for line in SCORED.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        if len(cached) == len(pool) and all("llm_score" in row for row in cached):
            pool = cached
            print(f"loaded score cache {SCORED.name}", flush=True)
    if not all("llm_score" in row for row in pool):
        batches = [pool[start:start + BATCH] for start in range(0, len(pool), BATCH)]
        all_scores = []
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for number, scores in enumerate(executor.map(score_batch, batches), 1):
                all_scores.extend(scores)
                if number % 10 == 0:
                    print(f"scored {min(number * BATCH, len(pool))}/{len(pool)}", flush=True)
        for row, score in zip(pool, all_scores):
            row["llm_score"] = score
        SCORED.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pool),
                          encoding="utf-8")
    all_scores = [row["llm_score"] for row in pool]
    print("score distribution:", {score: all_scores.count(score) for score in sorted(set(all_scores))})

    # Prefer high LLM scores, then style similarity; MMR keeps near-duplicates out.
    pool.sort(key=lambda row: (row["llm_score"], row.get("quality") or 0), reverse=True)
    curated_quotes = {item["quote"] for item in curated}
    pool = [row for row in pool if row["quote"] not in curated_quotes]
    high_quality = [row for row in pool if row["llm_score"] >= 7]
    if len(high_quality) >= TARGET - len(curated):
        pool = high_quality
        print(f"using llm_score>=7 pool: {len(pool)}", flush=True)
    embedder = load_expression_embedder()
    vectors = np.asarray(embedder.embed_documents([row["quote"] for row in pool]),
                         dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
    curated_vectors = np.asarray([embedder.embed_documents([row["quote"]])[0] for row in curated],
                                 dtype=np.float32)
    curated_vectors /= np.linalg.norm(curated_vectors, axis=1, keepdims=True) + 1e-9
    max_similarity = ((vectors @ curated_vectors.T).max(axis=1)
                      if len(curated_vectors) else np.zeros(len(pool), dtype=np.float32))

    selected, per_dialogue, per_technique = [], {}, {}
    selected_quotes = set()
    target_lccc = TARGET - len(curated)
    technique_cap, dialogue_cap = 180, 2
    while len(selected) < target_lccc:
        best_index, best_value = None, -1e9
        for index, row in enumerate(pool):
            if max_similarity[index] > 0.95:
                continue
            dialogue_key = row["source_id"].rsplit(":", 1)[0]
            if per_dialogue.get(dialogue_key, 0) >= dialogue_cap:
                continue
            if per_technique.get(row["technique"], 0) >= technique_cap:
                continue
            value = (0.65 * (row["llm_score"] / 10.0)
                     + 0.20 * float(row.get("quality") or 0)
                     - 0.35 * float(max_similarity[index]))
            if value > best_value:
                best_index, best_value = index, value
        if best_index is None:
            if technique_cap < 500:
                technique_cap += 80
                continue
            if dialogue_cap < 6:
                dialogue_cap += 1
                continue
            break
        chosen = pool[best_index]
        dialogue_key = chosen["source_id"].rsplit(":", 1)[0]
        per_dialogue[dialogue_key] = per_dialogue.get(dialogue_key, 0) + 1
        per_technique[chosen["technique"]] = per_technique.get(chosen["technique"], 0) + 1
        selected_quotes.add(chosen["quote"])
        selected.append({
            "quote": chosen["quote"],
            "context": chosen["context"],
            "technique": chosen["technique"],
            "situation": chosen["situation"],
            "source": chosen["source"],
            "source_id": chosen["source_id"],
            "source_title": chosen["source_title"],
            "quality": chosen.get("quality") or 0.0,
            "llm_score": chosen.get("llm_score"),
        })
        max_similarity = np.maximum(max_similarity, vectors @ vectors[best_index])
        if len(selected) % 100 == 0:
            print(f"selected {len(selected)}/{target_lccc}", flush=True)

    if len(selected) < target_lccc:
        for chosen in pool:
            if len(selected) >= target_lccc:
                break
            if chosen["quote"] in selected_quotes:
                continue
            selected_quotes.add(chosen["quote"])
            selected.append({
                "quote": chosen["quote"],
                "context": chosen["context"],
                "technique": chosen["technique"],
                "situation": chosen["situation"],
                "source": chosen["source"],
                "source_id": chosen["source_id"],
                "source_title": chosen["source_title"],
                "quality": chosen.get("quality") or 0.0,
                "llm_score": chosen.get("llm_score"),
            })

    combined = []
    for item in curated:
        combined.append({**item, "quality": 1.0})
    combined.extend(selected)
    OUTPUT.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in combined),
                      encoding="utf-8")
    print(f"wrote {len(combined)} -> {OUTPUT}")
    print("techniques:", sorted(per_technique.items(), key=lambda item: -item[1]))
    print("scores:", sorted({row.get("llm_score") for row in selected}))
    print("sample:")
    for row in (selected[:15] + selected[-15:]):
        print(f"  {row.get('llm_score')} [{row['technique']}] {row['quote']}")


if __name__ == "__main__":
    main()
