#!/usr/bin/env python3
"""Compare legacy and See Now on recorded, non-private browser frames.

Default mode only runs local algorithms. --vlm is an explicit, bounded paid step.
Use the existing thinking venv (Pillow/imagehash/numpy are already installed).
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASELINE = "e500ac60b4df30b4e8a624f5a7a53568f710d768"
PROMPT = (
    "请只依据图片中实际可辨认的内容，简洁记录画面的先后变化、页面标题、"
    "短暂提示、任务或告警编号、列表中特别标出的事项，以及配置详情中的版本号、"
    "校验码、重试间隔、备注和发布批次。拼图包含多帧时按帧号或时间顺序理解。"
    "看不清或没有看到的内容写未见，不猜测，不补全。控制在约350字以内。"
)


def dump(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_legacy():
    """Execute only two pure functions extracted verbatim from the Git baseline."""
    import imagehash
    from PIL import Image
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:realtime_screen_analysis_standalone.py"], cwd=ROOT, text=True)
    wanted = {"compute_change_ratio", "build_composite_left_to_right"}
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"imagehash": imagehash, "Image": Image, "pyautogui": None, "sys": sys}
    exec(compile(tree, f"git:{BASELINE}:realtime_screen_analysis_standalone.py", "exec"), namespace)
    return namespace, source


def legacy(manifest, recording, out):
    from PIL import Image
    functions, source = load_legacy()
    (out / "legacy-baseline.py.txt").write_text(source, encoding="utf-8")
    history, samples, outputs = [], [], []
    due = 0.0
    for frame in manifest["frames"]:
        if frame["t"] < due:
            continue
        due += 10.0
        with Image.open(recording / frame["path"]) as original:
            image = original.convert("RGB").resize((original.width // 2, original.height // 2), Image.Resampling.LANCZOS)
        ratio = functions["compute_change_ratio"](history[-1][1] if history else None, image)
        accepted = not history or ratio >= .15
        samples.append({"t": frame["t"], "frameIndex": frame["index"], "changeRatio": ratio, "accepted": accepted})
        if accepted:
            used = history[-2:] + [(frame, image)] if len(history) >= 2 else [(frame, image)]
            rendered = functions["build_composite_left_to_right"](*(im for _, im in used)) if len(used) == 3 else image
            name = f"legacy-{len(outputs) + 1:02d}.jpg"
            rendered.save(out / name, quality=75)
            outputs.append({"path": name, "emitted_t": frame["t"], "frames": [{"t": f["t"], "sourceIndex": f["index"]} for f, _ in used], "width": rendered.width, "height": rendered.height, "bytes": (out / name).stat().st_size})
        history = (history + [(frame, image)])[-2:]
    return {"strategy": "legacy", "baselineCommit": BASELINE, "baselineSha256": hashlib.sha256(source.encode()).hexdigest(), "intervalSeconds": 10, "changeThreshold": .15, "scale": .5, "imageEncoding": "JPEG quality 75", "samplingTiming": "idealized: ignores synchronous VLM latency before the original sleep", "activeWindow": "not included: baseline uses Windows GetForegroundWindow and this capture is isolated macOS browser content", "samples": samples, "outputs": outputs}


def see_now(manifest, recording, out, args):
    from PIL import Image
    from common.see_now import DESKTOP_OPTIONS, SeeNowClient
    from common.screen_runtime import fit_frame
    options = {**DESKTOP_OPTIONS, "maxWaitSec": args.max_wait}
    source_paths = [ROOT / p for p in ("common/see_now.py", "common/screen_runtime.py", "integrations/see_now/bridge.mjs")]
    source_paths += sorted((ROOT / "vendor/see_now/src").glob("*.js"))
    source_before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    outputs, samples, sequences = [], [], set()
    first = manifest["frames"][0]["t"]
    start = time.monotonic()
    due = first
    with SeeNowClient(args.width, args.height, args.fps, options=options, timeout=10) as client:
        def save(sheet, reason="automatic"):
            if not sheet or sheet["sequence"] in sequences:
                return
            sequences.add(sheet["sequence"])
            name = f"see-now-{len(outputs) + 1:02d}.png"
            (out / name).write_bytes(sheet["png"])
            with Image.open(io.BytesIO(sheet["png"])) as image:
                dimensions = {"width": image.width, "height": image.height}
            meta = {k: v for k, v in sheet.items() if k != "png"}
            meta.update(path=name, emitted_t=first + time.monotonic() - start, bytes=len(sheet["png"]), outputReason=reason, **dimensions)
            for frame in meta["frames"]:
                frame["t"] = frame["wall"] / 1000 - manifest["epoch"]
                frame["sourceIndex"] = min(manifest["frames"], key=lambda x: abs(x["t"] - frame["t"]))["index"]
            outputs.append(meta)

        # Start timing after Node startup so it is not counted as delivery latency.
        start = time.monotonic()
        for frame in manifest["frames"]:
            if frame["t"] + .001 < due:
                continue
            due += 1 / args.fps
            target = start + frame["t"] - first
            while time.monotonic() < target:
                save(client.take())
                time.sleep(min(.01, max(0, target - time.monotonic())))
            with Image.open(recording / frame["path"]) as original:
                image = fit_frame(original, (args.width, args.height))
            accepted = client.push(image, captured_at=manifest["epoch"] + frame["t"], sample_time=frame["t"] - first)
            samples.append({"t": frame["t"], "frameIndex": frame["index"], "enqueued": accepted})
            if len(samples) == 1:
                # ScreenWorker explicitly delivers its first successful capture.
                save(client.flush(timeout=10), "production_initial_flush")
            save(client.take())
        save(client.flush(timeout=10), "benchmark_end_flush")
        save(client.take())
        client_stats = client.health()
    source_after = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    return {"strategy": "see_now", "fps": args.fps, "canvas": [args.width, args.height], "options": options, "samplingTiming": "real-time replay with recorded capture timestamps", "clientStats": client_stats, "sourceSha256": source_before, "sourceStableDuringReplay": source_before == source_after, "samples": samples, "outputs": outputs}


def event_metrics(manifest, result):
    events = manifest["events"]
    intervals = []
    phases = [e for e in events if e["kind"] == "phase"]
    for i, e in enumerate(phases):
        intervals.append({"name": e["name"], "start": e["elapsed"], "end": phases[i + 1]["elapsed"] if i + 1 < len(phases) else manifest["durationSeconds"] + .5})
    toast = next(e for e in events if e["kind"] == "toast-start")
    toast_end = next(e for e in events if e["kind"] == "toast-end")
    intervals.append({"name": "toast", "start": toast["elapsed"], "end": toast_end["elapsed"]})
    normal_outputs = [o for o in result["outputs"] if o.get("outputReason") != "benchmark_end_flush"]
    for interval in intervals:
        hits = [(o, f) for o in normal_outputs for f in o["frames"] if interval["start"] <= f["t"] <= interval["end"]]
        interval.update(selected=bool(hits), sourceFrames=sum(interval["start"] <= f["t"] <= interval["end"] for f in manifest["frames"]), selectedFrames=[f["sourceIndex"] for _, f in hits], firstSheet=min((o["path"] for o, _ in hits), default=None), eventToSheetSeconds=min((o["emitted_t"] - interval["start"] for o, _ in hits), default=None))
    delays = [o["emitted_t"] - f["t"] for o in normal_outputs for f in o["frames"]]
    result["eventCoverage"] = intervals
    result["summary"] = {"sourceFrames": manifest["frameCount"], "sampledFrames": len(result["samples"]), "uniqueSelectedFrames": len({f["sourceIndex"] for o in normal_outputs for f in o["frames"]}), "outputImages": len(normal_outputs), "excludedBenchmarkEndFlushImages": len(result["outputs"]) - len(normal_outputs), "outputBytes": sum(o["bytes"] for o in normal_outputs), "selectedEvents": sum(i["selected"] for i in intervals), "totalEvents": len(intervals), "sampleToSheetSecondsMedian": statistics.median(delays) if delays else None, "sampleToSheetSecondsMax": max(delays, default=None)}
    return result


def real_vision(results, out, limit):
    from common.model_usage import capture_model_usage
    from common.screen_runtime import build_sheet_prompt
    from vlm_client import call_vision
    os.environ["BINES_MODEL_USAGE_PATH"] = str(out / "model-usage.sqlite")
    expected = ["TASK-417", "Q7M", "OPS-624", "AL-305", "ZK-581", "R7.3.19", "K8N-642", "17秒", "紫杉计划已启用", "BATCH-908", "READY-852"]
    responses = []
    for result in results:
        normal_outputs = [o for o in result["outputs"] if o.get("outputReason") != "benchmark_end_flush"]
        for output in normal_outputs[:limit]:
            events = []
            payload = base64.b64encode((out / output["path"]).read_bytes()).decode()
            sheet = dict(output)
            if not sheet.get("cells"):
                count = len(sheet["frames"])
                sheet["cells"] = [{"px": index * sheet["width"] // count, "py": 0,
                                   "pw": sheet["width"] // count, "ph": sheet["height"]}
                                  for index in range(count)]
            prompt = build_sheet_prompt(sheet, {"user_input": PROMPT})
            started = time.time()
            with capture_model_usage(events.append):
                response = call_vision(payload, prompt, max_tokens=600, retries=1, timeout=60, proxies={"http": None, "https": None}, missing_key_message="[missing model key]", empty_message="[empty model response]")
            final_events = [event for event in events if event.get("status") != "started"]
            compact_response = re.sub(r"\s+", "", response.casefold())
            record = {"strategy": result["strategy"], "image": output["path"], "imageSha256": hashlib.sha256((out / output["path"]).read_bytes()).hexdigest(), "prompt": prompt, "startedAt": started, "elapsedSeconds": time.time() - started, "response": response, "usageEvents": final_events, "recognizedMarkers": [marker for marker in expected if marker.casefold() in compact_response], "note": "Marker matching is supporting data; manual review distinguishes wrong or hallucinated text."}
            responses.append(record)
            dump(out / "vlm-results.json", {"prompt": PROMPT, "maxTokens": 600, "limitPerStrategy": limit, "results": responses})
            print(json.dumps({"strategy": record["strategy"], "image": record["image"], "elapsedSeconds": record["elapsedSeconds"], "status": [e["status"] for e in final_events], "usage": [e.get("usage") for e in final_events]}, ensure_ascii=False), flush=True)
    return responses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--strategy", choices=["legacy", "see-now", "both"], default="both")
    parser.add_argument("--fps", type=float, default=4)
    parser.add_argument("--width", type=int, default=1260)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--max-wait", type=float, default=10)
    parser.add_argument("--vlm", action="store_true", help="Explicitly make a bounded real model call batch")
    parser.add_argument("--vlm-only", action="store_true", help="Reuse saved strategy JSON, then run --vlm")
    parser.add_argument("--limit-per-strategy", type=int, default=4, choices=range(1, 7))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text())
    recording = args.manifest.parent
    if manifest.get("syntheticFrames") is not False:
        raise SystemExit("This benchmark requires a manifest explicitly identifying real browser frames")
    results = []
    strategies = ["legacy", "see-now"] if args.strategy == "both" else [args.strategy]
    for strategy in strategies:
        result_path = args.out / f"{strategy}-results.json"
        if args.vlm_only:
            result = json.loads(result_path.read_text())
        else:
            result = legacy(manifest, recording, args.out) if strategy == "legacy" else see_now(manifest, recording, args.out, args)
            event_metrics(manifest, result)
            result["sourceManifestSha256"] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
            dump(result_path, result)
        results.append(result)
        print(json.dumps({"strategy": result["strategy"], **result["summary"]}, ensure_ascii=False), flush=True)
    if args.vlm:
        real_vision(results, args.out, args.limit_per_strategy)


if __name__ == "__main__":
    main()
