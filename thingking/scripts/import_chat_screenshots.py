"""Extract candidate style quotes from public chat screenshots.

Step 1: this script OCRs screenshots and writes a candidate JSONL.
Step 2: review candidates, then run import_expression_corpus.py on the file.
No screenshot is imported automatically.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "thingking" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from common.env import read_env

for key, value in read_env(ROOT / ".env").items():
    os.environ.setdefault(key, value)

from memory.expression_learning import _SITUATION_CUES  # noqa: E402
from vlm_client import call_vision  # noqa: E402

DEFAULT_OUTPUT = ROOT / "thingking" / "data" / "expression_corpus" / "screenshot_candidates.jsonl"
PROMPT = """这是聊天记录截图。逐字提取清晰可读的对话，不补全、不猜测、不总结。
只输出 JSON 数组，元素为 {"speaker":"A"/"B"/"对方"/"我","text":"原话"}。
忽略时间、状态栏、输入框、点赞和无关 UI；看不清的文本直接省略。"""
_MARKERS = ("哈哈", "笑死", "？", "！", "…", "才", "嘛", "呀", "哼", "无语", "真的", "不是", "算了", "服了")
_NEGATIVE_MARKERS = ("想不出", "被猜中", "遮一下", "敷衍", "少装", "装什么", "你也配", "就这", "笑死你", "破防了")


def parse_dialogue(raw):
    start, end = str(raw or "").find("["), str(raw or "").rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(str(raw)[start:end + 1])
    except (TypeError, ValueError):
        return []
    lines = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            lines.append({"speaker": str(item.get("speaker") or "").strip(), "text": text})
    return lines


def quality_score(text):
    score = 0.0
    length = len(text)
    if 4 <= length <= 30:
        score += 1.0
    elif length <= 45:
        score += 0.5
    score += min(1.5, 0.25 * sum(marker in text for marker in _MARKERS))
    score -= 0.5 * sum(marker in text for marker in _NEGATIVE_MARKERS)
    if not re.search(r"https?://|\d{5,}", text):
        score += 0.5
    return max(0.0, score)


def _labels(previous, reply):
    if any(cue in previous for cue in _SITUATION_CUES["praised"]):
        return "understated_pride", "praised"
    if any(cue in previous for cue in _SITUATION_CUES["teased"]):
        return "light_teasing", "teased"
    if any(cue in previous for cue in _SITUATION_CUES["upset"]):
        return "warm_direct", "upset"
    if any(cue in previous for cue in _SITUATION_CUES["celebration"]):
        return "warm_direct", "celebration"
    return ("short_reaction" if len(reply) <= 8 else "specific_response"), "casual"


def candidates(lines, *, source, source_id, max_quotes=10, min_score=1.5):
    result = []
    for index in range(1, len(lines)):
        previous, current = lines[index - 1], lines[index]
        text = current["text"]
        if not 4 <= len(text) <= 45:
            continue
        score = quality_score(text)
        if score < min_score:
            continue
        technique, situation = _labels(previous["text"], text)
        result.append({
            "quote": text[:120],
            "context": f'{previous["speaker"]}: {previous["text"]}\n{current["speaker"]}: {text}',
            "technique": technique,
            "situation": situation,
            "source": source,
            "source_id": source_id,
            "source_title": "公开聊天截图",
            "quality": round(score, 2),
        })
    result.sort(key=lambda item: item["quality"], reverse=True)
    return result[:max_quotes]


def _image_input(value):
    path = Path(value)
    if path.exists():
        return base64.b64encode(path.read_bytes()).decode()
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", nargs="+", required=True,
                        help="本地截图路径或公开图片 URL")
    parser.add_argument("--source", default="", help="截图所在公开页面，便于追溯")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-quotes", type=int, default=10)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with args.output.open("w", encoding="utf-8") as handle:
        for value in args.images:
            raw = call_vision(_image_input(value), PROMPT, max_tokens=3000, retries=1)
            lines = parse_dialogue(raw)
            if not lines:
                print(f"{value}: 未提取到可读对话")
                continue
            source_id = "screenshot:" + hashlib.sha256(
                (args.source or value).encode("utf-8")).hexdigest()[:24]
            items = candidates(lines, source=args.source or value, source_id=source_id,
                               max_quotes=args.max_quotes)
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            written += len(items)
            print(f"{value}: {len(lines)} 行，候选 {len(items)} 条")
    print(f"done: {written} 条候选 -> {args.output}")
    print("先人工挑一遍，再运行 import_expression_corpus.py --corpus 该文件")


if __name__ == "__main__":
    main()
