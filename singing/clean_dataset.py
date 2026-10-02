"""清洗 so-vits-svc 训练集：剔除过短/过静/削波/异常响度，输出到新 raw 目录。"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf


def analyze(path: Path):
    x, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if getattr(x, "ndim", 1) == 2:
        x = x.mean(axis=1)
    dur = len(x) / sr if sr else 0
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(x**2) + 1e-12))
    rms_db = 20 * np.log10(rms + 1e-12)
    return {
        "path": path,
        "sr": sr,
        "dur": dur,
        "peak": peak,
        "rms": rms,
        "rms_db": rms_db,
        "channels": 1 if getattr(x, "ndim", 1) == 1 else x.shape[-1],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--dst", type=Path, required=True)
    parser.add_argument("--min-dur", type=float, default=0.6)
    parser.add_argument("--max-dur", type=float, default=20.0)
    parser.add_argument("--min-rms-db", type=float, default=-40.0)
    parser.add_argument("--max-rms-db", type=float, default=-8.0)
    parser.add_argument("--peak-clip", type=float, default=0.99)
    args = parser.parse_args()

    files = sorted(args.src.glob("*.wav"))
    kept, dropped = [], []
    args.dst.mkdir(parents=True, exist_ok=True)
    for path in files:
        try:
            info = analyze(path)
        except Exception as e:
            dropped.append({"file": path.name, "reason": f"read_error:{e}"})
            continue
        reason = None
        if info["dur"] < args.min_dur:
            reason = f"too_short:{info['dur']:.2f}"
        elif info["dur"] > args.max_dur:
            reason = f"too_long:{info['dur']:.2f}"
        elif info["peak"] >= args.peak_clip:
            reason = f"clipped:{info['peak']:.3f}"
        elif info["rms_db"] < args.min_rms_db:
            reason = f"too_quiet:{info['rms_db']:.1f}"
        elif info["rms_db"] > args.max_rms_db:
            reason = f"too_loud:{info['rms_db']:.1f}"
        if reason:
            dropped.append({"file": path.name, "reason": reason, **{k: info[k] for k in ("dur", "peak", "rms_db")}})
            continue
        shutil.copy2(path, args.dst / path.name)
        kept.append({"file": path.name, "dur": round(info["dur"], 3), "rms_db": round(info["rms_db"], 2)})

    report = {
        "src": str(args.src),
        "dst": str(args.dst),
        "kept": len(kept),
        "dropped": len(dropped),
        "kept_minutes": round(sum(x["dur"] for x in kept) / 60, 2),
        "drop_reasons": {},
    }
    for row in dropped:
        key = row["reason"].split(":")[0]
        report["drop_reasons"][key] = report["drop_reasons"].get(key, 0) + 1
    (args.dst.parent / "clean_report.json").write_text(
        json.dumps({"summary": report, "dropped": dropped[:200]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
