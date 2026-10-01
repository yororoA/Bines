"""把换声后的主唱干声重新加混响，并与伴奏/和声混音。

运行环境：singing venv（pedalboard、soundfile）。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 44100


def _load(path: Path, channels: int = 2) -> np.ndarray:
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != SAMPLE_RATE:
        from pedalboard import Resample
        audio = Resample(target_sample_rate=SAMPLE_RATE)(audio.T, sr).T
    if audio.shape[1] < channels:
        audio = np.repeat(audio[:, :1], channels, axis=1)
    return audio[:, :channels]


def _rms(audio: np.ndarray) -> float:
    # 只按有声部分计算，避免长静音拉低估计值
    frame = audio.mean(axis=1)
    voiced = np.abs(frame) > 1e-3
    return float(np.sqrt(np.mean(frame[voiced] ** 2))) if voiced.any() else 0.0


def _fit(audio: np.ndarray, length: int) -> np.ndarray:
    if len(audio) >= length:
        return audio[:length]
    return np.pad(audio, ((0, length - len(audio)), (0, 0)))


def add_reverb(vocal: np.ndarray, wet: float, room: float) -> np.ndarray:
    from pedalboard import Pedalboard, Reverb, HighpassFilter

    board = Pedalboard([
        HighpassFilter(cutoff_frequency_hz=80),
        Reverb(room_size=room, damping=0.5, wet_level=wet, dry_level=1.0 - wet * 0.5, width=0.9),
    ])
    return board(vocal.T, SAMPLE_RATE).T


KEPT_STEMS = ("instrumental", "backing", "lead_dry", "converted")


def _stem(work_dir: Path, name: str) -> Path:
    for suffix in (".wav", ".flac"):
        if (work_dir / f"{name}{suffix}").is_file():
            return work_dir / f"{name}{suffix}"
    return work_dir / f"{name}.wav"


def compact(work_dir: Path) -> None:
    """把留作重新混音的 WAV 转成 24 位 FLAC，体积约为三分之一。"""
    for name in KEPT_STEMS:
        wav = work_dir / f"{name}.wav"
        if wav.is_file():
            data, sr = sf.read(str(wav), dtype="float32")
            sf.write(str(work_dir / f"{name}.flac"), np.clip(data, -1.0, 1.0), sr, subtype="PCM_24")
            wav.unlink()


def mix(work_dir: Path, converted: Path, out: Path, reverb_wet: float, reverb_room: float,
        backing_gain: float, vocal_gain_db: float, target_lufs: float = -14.0) -> dict:
    instrumental = _load(_stem(work_dir, "instrumental"))
    length = len(instrumental)
    vocal = _fit(_load(converted), length)
    reference = _load(_stem(work_dir, "lead_dry"))

    target, current = _rms(reference), _rms(vocal)
    gain = (target / current if current > 0 else 1.0) * 10 ** (vocal_gain_db / 20)
    vocal = add_reverb(vocal * gain, reverb_wet, reverb_room)

    mixed = instrumental + _fit(vocal, length)
    if backing_gain > 0 and _stem(work_dir, "backing").is_file():
        mixed += _fit(_load(_stem(work_dir, "backing")), length) * backing_gain
    peak = float(np.max(np.abs(mixed))) or 1.0
    # 原曲经过母带压限，分离出的各轨直接相加会超过满幅；用限幅器而不是整体降音量
    from pedalboard import Limiter
    mixed = Limiter(threshold_db=-1.0, release_ms=120)(mixed.T, SAMPLE_RATE).T
    if target_lufs is not None:
        import pyloudnorm as pyln
        loudness = pyln.Meter(SAMPLE_RATE).integrated_loudness(mixed)
        if np.isfinite(loudness) and loudness > target_lufs:
            mixed = mixed * 10 ** ((target_lufs - loudness) / 20)
    mixed = np.clip(mixed, -0.995, 0.995)

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".mp3":
        # compression_level=0 即 LAME 最高质量 VBR（约 V0）
        sf.write(str(out), mixed, SAMPLE_RATE, format="MP3", compression_level=0.0)
    else:
        sf.write(str(out), mixed, SAMPLE_RATE)
    return {"vocal_gain": round(gain, 3), "peak_before_limit": round(peak, 3), "path": str(out)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path, help="separate.py 的输出目录")
    parser.add_argument("converted", type=Path, help="svc_convert.py 输出的 converted.wav")
    parser.add_argument("out", type=Path)
    parser.add_argument("--reverb-wet", type=float, default=0.18)
    parser.add_argument("--reverb-room", type=float, default=0.45)
    parser.add_argument("--backing-gain", type=float, default=1.0)
    parser.add_argument("--vocal-gain-db", type=float, default=0.0)
    parser.add_argument("--target-lufs", type=float, default=-14.0)
    parser.add_argument("--compact", action="store_true", help="混音后把保留的中间文件转成 FLAC")
    args = parser.parse_args()
    info = mix(args.work_dir, args.converted, args.out, args.reverb_wet, args.reverb_room,
               args.backing_gain, args.vocal_gain_db, args.target_lufs)
    if args.compact:
        compact(args.work_dir)
    print("MIX_DONE " + json.dumps(info, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.exit(main())
