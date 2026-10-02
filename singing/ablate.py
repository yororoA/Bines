"""推理参数消融：同一干声、多种 (checkpoint / noise_scale / 浅扩散) 组合。

在 so-vits-svc 环境中运行。输出每组的完整换声干声 + 一段固定高能量 clip，便于对比嘶哑。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
import time
from pathlib import Path


def _seed_everything(seed: int) -> None:
    import numpy as np
    import torch

    random.seed(seed)
    np.random.seed(seed % (2**32))
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def _convert_one(svc, src: Path, out_wav: Path, *, speaker, transpose, slice_db, pad_seconds,
                 noise_scale, f0_predictor, k_step, seed) -> dict:
    import numpy as np
    import soundfile
    from inference import slicer
    from inference.infer_tool import pad_array

    chunks = slicer.cut(str(src), db_thresh=slice_db)
    audio_data, audio_sr = slicer.chunks2audio(str(src), chunks)
    target_sr = svc.target_sample
    pieces = []
    started = time.time()
    for index, (is_silence, data) in enumerate(audio_data):
        length = int(np.ceil(len(data) / audio_sr * target_sr))
        if is_silence:
            piece = np.zeros(length)
        else:
            _seed_everything(seed * 100003 + index)
            pad_len = int(audio_sr * pad_seconds)
            padded = np.concatenate([np.zeros([pad_len]), data, np.zeros([pad_len])])
            raw = io.BytesIO()
            soundfile.write(raw, padded, audio_sr, format="wav")
            raw.seek(0)
            out_audio, _, _ = svc.infer(
                speaker, transpose, raw,
                noice_scale=noise_scale,
                f0_predictor=f0_predictor,
                k_step=k_step,
                loudness_envelope_adjustment=1.0,
            )
            piece = out_audio.cpu().numpy()
            out_pad = int(target_sr * pad_seconds)
            piece = pad_array(piece[out_pad:-out_pad], length)
        pieces.append(piece)
        svc.clear_empty()
    full = np.concatenate(pieces) if pieces else np.zeros(0)
    soundfile.write(out_wav, full, target_sr)
    return {
        "seconds": round(time.time() - started, 1),
        "duration": round(len(full) / target_sr, 1),
        "segments": len(audio_data),
        "path": str(out_wav),
    }


def _clip_window(mono, sr, seconds=40):
    import numpy as np

    n = len(mono) // sr
    if n < seconds:
        return 0, len(mono)
    energy = (mono[: n * sr] ** 2).reshape(n, sr).sum(axis=1)
    csum = np.concatenate([[0], np.cumsum(energy)])
    best = int(np.argmax(csum[seconds:] - csum[:-seconds]))
    return best * sr, (best + seconds) * sr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="主唱干声 wav/flac")
    parser.add_argument("out_dir")
    parser.add_argument("--sovits-dir", required=True)
    parser.add_argument("--config", default="logs/44k/config.json")
    parser.add_argument("--diffusion-model", default="logs/44k/diffusion/model_100000.pt")
    parser.add_argument("--diffusion-config", default="logs/44k/diffusion/config.yaml")
    parser.add_argument("--speaker", default="ATRI")
    parser.add_argument("--f0-predictor", default="rmvpe")
    parser.add_argument("--seed", type=int, default=20260207)
    parser.add_argument("--clip-seconds", type=int, default=40)
    args = parser.parse_args()

    sovits_dir = Path(args.sovits_dir).resolve()
    os.chdir(sovits_dir)
    sys.path.insert(0, str(sovits_dir))

    import numpy as np
    import soundfile
    from inference.infer_tool import Svc

    src = Path(args.input).resolve()
    # so-vits slicer/librosa 对 flac 支持不一，统一成临时 wav
    work = Path(args.out_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    src_wav = work / "_lead_dry.wav"
    data, sr = soundfile.read(str(src), dtype="float32")
    soundfile.write(src_wav, data, sr)
    mono = data.mean(axis=1) if getattr(data, "ndim", 1) == 2 else data
    clip_start, clip_end = _clip_window(mono, sr, args.clip_seconds)
    soundfile.write(work / f"0_source_clip_{clip_start // sr}s.wav", data[clip_start:clip_end], sr)

    # (label, model, shallow_diffusion, noise_scale, k_step)
    variants = [
        ("1_baseline_G36000_ns04_k100", "logs/44k/G_36000.pth", True, 0.4, 100),
        ("2_G36000_ns03_k100", "logs/44k/G_36000.pth", True, 0.3, 100),
        ("3_G36000_ns02_k100", "logs/44k/G_36000.pth", True, 0.2, 100),
        ("4_G36000_ns04_k30", "logs/44k/G_36000.pth", True, 0.4, 30),
        ("5_G36000_ns02_k30", "logs/44k/G_36000.pth", True, 0.2, 30),
        ("6_G36000_ns04_noshd", "logs/44k/G_36000.pth", False, 0.4, 100),
        ("7_G36000_ns02_noshd", "logs/44k/G_36000.pth", False, 0.2, 100),
        ("8_G32000_ns04_k100", "logs/44k/G_32000.pth", True, 0.4, 100),
        ("9_G32000_ns02_k30", "logs/44k/G_32000.pth", True, 0.2, 30),
    ]

    results = []
    current_model = None
    svc = None
    for label, model, shallow, noise, k_step in variants:
        if current_model != (model, shallow):
            if svc is not None:
                svc.unload_model()
                del svc
            print(f"LOAD {model} shallow={shallow}", flush=True)
            svc = Svc(
                model, args.config,
                diffusion_model_path=args.diffusion_model,
                diffusion_config_path=args.diffusion_config,
                shallow_diffusion=shallow,
                cluster_model_path="",
            )
            current_model = (model, shallow)
        out_full = work / f"{label}_full.wav"
        print(f"RUN {label}", flush=True)
        info = _convert_one(
            svc, src_wav, out_full,
            speaker=args.speaker, transpose=0, slice_db=-40, pad_seconds=0.5,
            noise_scale=noise, f0_predictor=args.f0_predictor, k_step=k_step, seed=args.seed,
        )
        converted, csr = soundfile.read(str(out_full), dtype="float32")
        # clip 按源曲时间轴对齐（换声后采样率可能不同，用秒数）
        c_start = int(clip_start / sr * csr)
        c_end = int(clip_end / sr * csr)
        soundfile.write(work / f"{label}_clip.wav", converted[c_start:c_end], csr)
        row = {
            "label": label, "model": model, "shallow_diffusion": shallow,
            "noise_scale": noise, "k_step": k_step, **info,
        }
        results.append(row)
        print("DONE " + json.dumps(row), flush=True)

    (work / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    src_wav.unlink(missing_ok=True)
    print("ABLATION_DONE " + str(work), flush=True)
    if svc is not None:
        svc.unload_model()
    os._exit(0)


if __name__ == "__main__":
    main()
