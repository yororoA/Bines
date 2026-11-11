"""Build a ~1K expression corpus from curated posts plus filtered LCCC turns."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "thingking" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("HF_HOME", str(ROOT / "thingking" / "models" / "hf-cache"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from memory.embedding import load_expression_embedder

CURATED = ROOT / "thingking" / "data" / "expression_corpus" / "xiaoheihe_qingtouyihe.jsonl"
# Intermediate candidate set; score_expression_pool.py writes the final
# expression_corpus_1k.jsonl after LLM quality scoring.
OUTPUT = ROOT / "thingking" / "data" / "expression_corpus" / "expression_corpus_1k_candidates.jsonl"
LCCC_GLOB = str(ROOT / "thingking" / "models" / "hf-cache" / "hub" / "datasets--silver--lccc"
                / "snapshots" / "*" / "lccc_base_*.jsonl.gz")
TARGET = 1000
TRAIN_LIMIT = int(os.environ.get("LCCC_TRAIN_LIMIT", "60000"))

ANCHORS = [
    ("撒娇但不直说，带一点嘴硬", "softener", "casual"),
    ("假装生气，轻轻抱怨对方", "gentle_protest", "teased"),
    ("俏皮地回怼或打趣对方", "light_teasing", "teased"),
    ("温柔地安慰或关心对方", "warm_direct", "upset"),
    ("被夸奖时害羞又开心", "understated_pride", "praised"),
    ("接住对方的话头，换个角度联想", "playful_reframe", "casual"),
    ("先用很短的语气词接住，再补一句", "short_reaction", "casual"),
    ("好奇地追问一个具体细节", "curious_followup", "casual"),
    ("轻巧地自嘲或认怂", "self_mock", "casual"),
    ("夸张地卖惨或放大情绪", "playful_exaggeration", "teased"),
    ("具体回应对方刚说的细节", "specific_response", "casual"),
    ("意思说到就留白，不追着解释", "leave_space", "parting"),
    ("自然使用语气词，不堆砌口头禅", "natural_particles", "casual"),
    ("句子长短随情绪变化", "varied_rhythm", "casual"),
]
ANCHOR_WEIGHTS = [1.0, 1.0, 1.0, 1.0, 0.9, 1.0, 0.8, 0.8, 0.9, 1.0, 0.7, 0.7, 0.7, 0.6]

BAD_FRAGMENTS = (
    "http", "www.", "微信", "威信", "qq", "QQ", "电话", "手机号", "加v", "代购",
    "淘宝", "天猫", "优惠", "招聘", "兼职", "广告", "转发", "微博", "热搜",
    "链接", "下载", "注册", "app", "APP", "网页链接", "展开全文",
    "男神", "女神", "明星", "粉丝", "采访", "娱乐圈", "综艺", "导演", "演员", "歌手",
    "层主", "楼主", "社长", "点赞", "回复", "互粉", "取关",
)
LOW_QUALITY = (
    "啥人都有", "什么态度", "用你在这说", "负能量", "说谎", "欠死", "狠一点",
    "你才搞笑", "层猪", "说教", "你懂什么", "少来这套",
)
TOXIC = (
    "傻逼", "操你", "草你", "妈的", "滚", "去死", "死全家", "贱", "婊", "嫖",
    "约炮", "裸聊", "色情", "黄图", "做爱", "上床", "奶子", "鸡巴", "阴道",
    "阴茎", "性交", "强奸", "自杀", "刀", "杀你", "弄死",
)
SOFT_MARKERS = ("呀", "嘛", "呢", "啦", "哼", "才", "人家", "喔", "哦", "诶", "哎")
PLAY_MARKERS = ("哈", "笑", "逗", "梗", "无语", "服了", "离谱", "救命")
QUESTION_MARKERS = ("？", "?")

_SPACE_CJK = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_SPACE_DIGIT = re.compile(r"(?<=\d)\s+(?=\d)")
_SPACE_PUNCT = re.compile(r"\s*([，。！？、；：,.!?;:])\s*")


def normalize_turn(turn) -> str:
    text = " ".join(turn) if isinstance(turn, list) else str(turn or "")
    text = _SPACE_CJK.sub("", text)
    text = _SPACE_DIGIT.sub("", text)
    text = _SPACE_PUNCT.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"@\S+|#\S+#", "", text)
    text = re.sub(r"\[[^\]]{1,12}\]", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)
    text = re.sub(r"([，。！？、；：,.!?;:])\1+", r"\1", text)
    return text.strip()


def acceptable(text: str) -> bool:
    if not 6 <= len(text) <= 32:
        return False
    if not re.search(r"[\u4e00-\u9fff]", text):
        return False
    if len(re.findall(r"[A-Za-z]", text)) / max(1, len(text)) > 0.5:
        return False
    if len(re.findall(r"\d", text)) >= 6:
        return False
    lowered = text.lower()
    if any(fragment.lower() in lowered for fragment in BAD_FRAGMENTS):
        return False
    if any(term in text for term in TOXIC):
        return False
    if any(term in text for term in LOW_QUALITY):
        return False
    # A usable chat line needs a speaker, a particle, or a clear emotional turn;
    # this rejects noun-phrase fragments such as "一脸正气" or "玩笑淡定".
    has_pronoun = any(part in text for part in ("你", "我", "他", "她", "咱", "大家", "人家"))
    has_particle = any(part in text for part in
                       ("呀", "嘛", "呢", "啦", "吧", "啊", "哦", "嗯", "哈", "呗",
                        "咯", "喔", "诶", "哎", "哼"))
    has_emotion = any(part in text for part in
                      SOFT_MARKERS + PLAY_MARKERS +
                      ("喜欢", "想", "爱", "讨厌", "气", "哭", "委屈", "心疼", "抱", "亲", "晚安"))
    if not (has_pronoun or has_particle or has_emotion):
        return False
    return True


def heuristic_bonus(text: str) -> float:
    bonus = 0.0
    if any(marker in text for marker in SOFT_MARKERS):
        bonus += 0.02
    if any(marker in text for marker in PLAY_MARKERS):
        bonus += 0.015
    if len(text) <= 16:
        bonus += 0.01
    if text.endswith(QUESTION_MARKERS) and not any(marker in text for marker in PLAY_MARKERS + SOFT_MARKERS):
        bonus -= 0.02
    return bonus


def load_candidates():
    rows = []
    for path in sorted(Path("/").glob(LCCC_GLOB.lstrip("/"))):
        split = ("train" if "train" in path.name else
                 "valid" if "valid" in path.name else "test")
        limit = TRAIN_LIMIT if split == "train" else None
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for row_number, line in enumerate(stream):
                if limit is not None and row_number >= limit:
                    break
                if not line.strip():
                    continue
                dialogue = json.loads(line)
                if not isinstance(dialogue, list):
                    continue
                turns = [normalize_turn(turn) for turn in dialogue]
                for index in range(1, len(turns)):
                    quote = turns[index]
                    if not acceptable(quote):
                        continue
                    context = turns[index - 1] if turns[index - 1] else ""
                    rows.append({
                        "quote": quote,
                        "context": (f"A: {context}\nB: {quote}" if context else f"B: {quote}"),
                        "source": "https://huggingface.co/datasets/silver/lccc",
                        "source_id": f"lccc:base:{split}:{row_number}:{index}",
                        "source_title": "LCCC base 过滤语料",
                        "dialogue": f"{split}:{row_number}",
                    })
    # Exact deduplicate, keeping the first occurrence.
    unique, seen = [], set()
    for row in rows:
        key = re.sub(r"\s+", "", row["quote"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def embed_all(embedder, texts, batch_size=64):
    vectors = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(embedder.embed_documents(texts[start:start + batch_size]))
        if start and start % (batch_size * 20) == 0:
            print(f"embedded {start}/{len(texts)}", flush=True)
    return np.asarray(vectors, dtype=np.float32)


def main():
    random.seed(7)
    curated = [json.loads(line) for line in CURATED.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    candidates = load_candidates()
    print(f"curated={len(curated)} lccc_candidates={len(candidates)}", flush=True)

    embedder = load_expression_embedder()
    anchor_vectors = np.asarray([embedder.embed_query(anchor) for anchor, _, _ in ANCHORS],
                                dtype=np.float32)
    anchor_vectors /= np.linalg.norm(anchor_vectors, axis=1, keepdims=True) + 1e-9
    weights = np.asarray(ANCHOR_WEIGHTS, dtype=np.float32)

    fingerprint = hashlib.sha256(
        "\n".join(row["quote"] for row in candidates).encode("utf-8")).hexdigest()[:16]
    cache_vectors = OUTPUT.with_name(f".lccc_vectors_{fingerprint}.npy")
    cache_rows = OUTPUT.with_name(f".lccc_rows_{fingerprint}.jsonl")
    quote_vectors = None
    if cache_vectors.is_file() and cache_rows.is_file():
        cached = [json.loads(line) for line in cache_rows.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        if len(cached) == len(candidates):
            candidates = cached
            quote_vectors = np.load(cache_vectors)
            print(f"loaded embedding cache {cache_vectors.name}", flush=True)
    if quote_vectors is None:
        quote_vectors = embed_all(embedder, [row["quote"] for row in candidates])
        np.save(cache_vectors, quote_vectors)
        cache_rows.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                      for row in candidates), encoding="utf-8")
    quote_vectors = np.asarray(quote_vectors, dtype=np.float32)
    quote_vectors /= np.linalg.norm(quote_vectors, axis=1, keepdims=True) + 1e-9
    similarity = quote_vectors @ anchor_vectors.T
    best_anchor = similarity.argmax(axis=1)
    style = (similarity * weights).max(axis=1)
    style = style + np.asarray([heuristic_bonus(row["quote"]) for row in candidates],
                               dtype=np.float32)

    order = np.argsort(-style)
    pool_path = OUTPUT.with_name("expression_pool_3000.jsonl")
    pool = []
    for index in order[:3000]:
        technique, situation = ANCHORS[best_anchor[index]][1:]
        row = candidates[index]
        pool.append({
            "quote": row["quote"],
            "context": row["context"],
            "technique": technique,
            "situation": situation,
            "source": row["source"],
            "source_id": row["source_id"],
            "source_title": row["source_title"],
            "quality": round(float(style[index]), 3),
        })
    pool_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                 for row in pool), encoding="utf-8")
    print(f"pool {len(pool)} -> {pool_path}", flush=True)
    threshold = 0.52
    eligible = [int(index) for index in order if style[index] >= threshold]
    if len(eligible) < TARGET - len(curated):
        threshold = 0.50
        eligible = [int(index) for index in order if style[index] >= threshold]
    print(f"eligible={len(eligible)} threshold={threshold}", flush=True)

    # Seed the diversity buffer with the curated lines so LCCC does not
    # duplicate their wording.
    curated_vectors = np.asarray([embedder.embed_documents([item["quote"]])[0]
                                  for item in curated], dtype=np.float32)
    curated_vectors /= np.linalg.norm(curated_vectors, axis=1, keepdims=True) + 1e-9
    max_similarity = np.zeros(len(candidates), dtype=np.float32)
    if len(curated_vectors):
        max_similarity = np.maximum(max_similarity,
                                    (quote_vectors @ curated_vectors.T).max(axis=1))

    selected = []
    per_dialogue = {}
    per_technique = {}
    technique_cap = 140
    target_lccc = max(0, TARGET - len(curated))
    while len(selected) < target_lccc:
        best_index, best_value = None, -1e9
        for index in eligible:
            if max_similarity[index] > 0.95:
                continue
            dialogue = candidates[index]["dialogue"]
            if per_dialogue.get(dialogue, 0) >= 2:
                continue
            technique, _ = ANCHORS[best_anchor[index]][1:]
            if per_technique.get(technique, 0) >= technique_cap:
                continue
            value = 0.72 * float(style[index]) - 0.23 * float(max_similarity[index])
            if value > best_value:
                best_index, best_value = index, value
        if best_index is None:
            if technique_cap < 400:
                technique_cap += 60
                continue
            break
        chosen = candidates[best_index]
        technique, situation = ANCHORS[best_anchor[best_index]][1:]
        per_dialogue[chosen["dialogue"]] = per_dialogue.get(chosen["dialogue"], 0) + 1
        per_technique[technique] = per_technique.get(technique, 0) + 1
        selected.append({
            "quote": chosen["quote"],
            "context": chosen["context"],
            "technique": technique,
            "situation": situation,
            "source": chosen["source"],
            "source_id": chosen["source_id"],
            "source_title": chosen["source_title"],
            "quality": round(float(style[best_index]), 3),
        })
        max_similarity = np.maximum(max_similarity,
                                    quote_vectors @ quote_vectors[best_index])
        if len(selected) % 100 == 0:
            print(f"selected {len(selected)}/{target_lccc}", flush=True)

    combined = curated + selected
    OUTPUT.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n"
                              for item in combined), encoding="utf-8")
    print(f"wrote {len(combined)} -> {OUTPUT}")
    print("techniques:", sorted(per_technique.items(), key=lambda item: -item[1]))
    print("sample:")
    for item in random.sample(combined, min(30, len(combined))):
        print(f"  {item['quality'] if 'quality' in item else '-'} "
              f"[{item['technique']}] {item['quote']}")


if __name__ == "__main__":
    main()
