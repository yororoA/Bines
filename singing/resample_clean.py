"""把清洗后的 raw wav 重采样到 44.1kHz、削峰归一、裁静音，写入 dataset/44k_*。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


def trim(x: np.ndarray, top_db: float = 40.0):
    if x.size == 0:
        return x
    frame = 1024
    hop = 256
    if len(x) < frame:
        return x
    energy = np.array([
        20 * np.log10(np.sqrt(np.mean(x[i:i + frame] ** 2) + 1e-12) + 1e-12)
        for i in range(0, len(x) - frame + 1, hop)
    ])
    thr = energy.max() - top_db
    keep = np.where(energy > thr)[0]
    if keep.size == 0:
        return x
    start = keep[0] * hop
    end = min(len(x), keep[-1] * hop + frame)
    return x[start:end]


def process_one(src: Path, dst: Path, target_sr: int = 44100):
    x, sr = sf.read(str(src), dtype="float32", always_2d=False)
    if getattr(x, "ndim", 1) == 2:
        x = x.mean(axis=1)
    x = trim(x)
    if sr != target_sr:
        from math import gcd
        g = gcd(sr, target_sr)
        x = resample_poly(x, target_sr // g, sr // g).astype(np.float32)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 1e-6:
        x = x / peak
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), x, target_sr, subtype="PCM_16")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    jobs = []
    for spk in sorted(p for p in args.in_dir.iterdir() if p.is_dir()):
        for wav in spk.glob("*.wav"):
            jobs.append((wav, args.out_dir / spk.name / wav.name))
    ok = err = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_one, s, d): s for s, d in jobs}
        for fut in as_completed(futs):
            try:
                fut.result()
                ok += 1
            except Exception as e:
                err += 1
                print("FAIL", futs[fut].name, e, flush=True)
            if (ok + err) % 200 == 0:
                print(f"progress {ok + err}/{len(jobs)}", flush=True)
    print(f"RESAMPLE_DONE ok={ok} err={err}", flush=True)


if __name__ == "__main__":
    main()
