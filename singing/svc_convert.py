"""用 so-vits-svc 把主唱干声转换成目标音色，可逐段输出以便边转边播。

运行环境：so-vits-svc 整合包自带的 Python 3.9（需在其安装目录下运行，内部依赖相对路径）。

与官方 Svc.slice_inference 的切片/拼接逻辑一致：按静音切成乐句，每个乐句独立推理。
区别只有两点：
1. 每个乐句推理前按 (seed, 乐句序号) 重置随机种子，结果与是否分段输出、输出几次都无关；
2. 每完成一个乐句就写出 seg_XXXX.wav 并打印一行 SEGMENT JSON，调用方可立即开始播放。
"""
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
    np.random.seed(seed % (2 ** 32))
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def convert(args) -> None:
    sovits_dir = Path(args.sovits_dir).resolve()
    os.chdir(sovits_dir)
    sys.path.insert(0, str(sovits_dir))

    import numpy as np
    import soundfile
    from inference import slicer
    from inference.infer_tool import Svc, pad_array

    load_start = time.time()
    svc = Svc(
        args.model, args.config,
        diffusion_model_path=args.diffusion_model,
        diffusion_config_path=args.diffusion_config,
        shallow_diffusion=args.shallow_diffusion,
        cluster_model_path="",
    )
    print("MODEL_LOADED " + json.dumps({"seconds": round(time.time() - load_start, 1)}), flush=True)

    src = Path(args.input).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = slicer.cut(str(src), db_thresh=args.slice_db)
    audio_data, audio_sr = slicer.chunks2audio(str(src), chunks)
    target_sr = svc.target_sample

    pieces = []
    position = 0
    started = time.time()
    for index, (is_silence, data) in enumerate(audio_data):
        length = int(np.ceil(len(data) / audio_sr * target_sr))
        if is_silence:
            piece = np.zeros(length)
        else:
            _seed_everything(args.seed * 100003 + index)
            pad_len = int(audio_sr * args.pad_seconds)
            padded = np.concatenate([np.zeros([pad_len]), data, np.zeros([pad_len])])
            raw = io.BytesIO()
            soundfile.write(raw, padded, audio_sr, format="wav")
            raw.seek(0)
            out_audio, _, _ = svc.infer(
                args.speaker, args.transpose, raw,
                noice_scale=args.noise_scale,
                f0_predictor=args.f0_predictor,
                k_step=args.k_step,
                loudness_envelope_adjustment=args.loudness_envelope_adjustment,
            )
            piece = out_audio.cpu().numpy()
            out_pad = int(target_sr * args.pad_seconds)
            piece = pad_array(piece[out_pad:-out_pad], length)
        seg_path = out_dir / f"seg_{index:04d}.wav"
        soundfile.write(seg_path, piece, target_sr)
        pieces.append(piece)
        print("SEGMENT " + json.dumps({
            "index": index,
            "total": len(audio_data),
            "start": round(position / target_sr, 3),
            "end": round((position + length) / target_sr, 3),
            "silence": bool(is_silence),
            "path": str(seg_path),
            "elapsed": round(time.time() - started, 2),
        }), flush=True)
        position += length
        svc.clear_empty()

    full = np.concatenate(pieces) if pieces else np.zeros(0)
    soundfile.write(out_dir / "converted.wav", full, target_sr)
    print("CONVERT_DONE " + json.dumps({
        "seconds": round(time.time() - started, 1),
        "duration": round(len(full) / target_sr, 1),
        "segments": len(audio_data),
        "path": str(out_dir / "converted.wav"),
    }), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("out_dir")
    parser.add_argument("--sovits-dir", required=True)
    parser.add_argument("--model", default="logs/44k/G_36000.pth")
    parser.add_argument("--config", default="logs/44k/config.json")
    parser.add_argument("--diffusion-model", default="logs/44k/diffusion/model_100000.pt")
    parser.add_argument("--diffusion-config", default="logs/44k/diffusion/config.yaml")
    parser.add_argument("--no-shallow-diffusion", dest="shallow_diffusion", action="store_false")
    parser.add_argument("--speaker", default="ATRI")
    parser.add_argument("--transpose", type=int, default=0)
    parser.add_argument("--f0-predictor", default="rmvpe")
    parser.add_argument("--k-step", type=int, default=100)
    parser.add_argument("--noise-scale", type=float, default=0.4)
    parser.add_argument("--slice-db", type=int, default=-40)
    parser.add_argument("--pad-seconds", type=float, default=0.5)
    parser.add_argument("--loudness-envelope-adjustment", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260207)
    convert(parser.parse_args())


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    sys.stderr.flush()
    # so-vits-svc 的依赖会留下非守护线程，正常退出会一直挂起
    os._exit(0)
