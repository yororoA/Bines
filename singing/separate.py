"""人声预处理：分离主唱 → 去和声 → 去混响，产出可直接送入 so-vits-svc 的干声。

运行环境：独立的 singing venv（audio-separator 需要 Python >= 3.10，so-vits-svc 环境是 3.9）。
输出目录结构（stage 名即文件名）：
    source.wav         统一格式后的原曲（44.1kHz 立体声）
    instrumental.wav   伴奏（原曲减去全部人声）
    vocals.wav         全部人声（主唱 + 和声，带混响）
    backing.wav        背景和声（带混响），混音时可选择加回
    lead_wet.wav       主唱（仍带混响）
    lead_dry.wav       主唱干声，送入换声
    lead_reverb.wav    从主唱里去掉的混响层（仅供参考）
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

VOCAL_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
KARAOKE_MODEL = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
SAFE_PEAK = 0.9
DEREVERB_MODELS = {
    "anvuew": "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt",
    "sucial_echo": "dereverb-echo_mel_band_roformer_sdr_13.4843_v2.ckpt",
}


def _pick(outputs, out_dir: Path, *stems: str) -> Path:
    """audio-separator 输出名形如 "<输入名>_(Stem)_<模型名>"，只认最后一个括号里的 stem。"""
    wanted = {s.lower() for s in stems}
    for name in outputs:
        tags = re.findall(r"\(([^()]+)\)", Path(name).name)
        if tags and tags[-1].lower() in wanted:
            return out_dir / Path(name).name
    raise RuntimeError(f"找不到 {stems} 对应的输出: {outputs}")


def _run(separator, model: str, src: Path, tmp: Path):
    # 输入改成短名，避免上一步的 stem 标签混进下一步的输出文件名
    staged = tmp / f"in{src.suffix}"
    shutil.copyfile(src, staged)
    separator.load_model(model_filename=model)
    try:
        return separator.separate(str(staged))
    finally:
        staged.unlink(missing_ok=True)


def prepare_input(src: Path, dest: Path) -> None:
    """统一成 44.1kHz 立体声 WAV：视频只取音轨，5.1 等多声道按 ffmpeg 标准矩阵下混。"""
    import subprocess
    import imageio_ffmpeg

    import numpy as np
    import soundfile as sf

    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_f32le", str(dest)],
        check=True,
    )
    # 下混后峰值可能超过满幅。先整体缩到 SAFE_PEAK 以下，分离时就不会触发
    # audio-separator 对各 stem 的单独归一化，伴奏与人声保持原曲的相对音量。
    audio, sr = sf.read(str(dest), dtype="float32")
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > SAFE_PEAK:
        sf.write(str(dest), audio * (SAFE_PEAK / peak), sr, subtype="FLOAT")


def separate(src: Path, out_dir: Path, model_dir: Path, dereverb: str = "anvuew") -> dict:
    import onnxruntime as ort
    if "CUDAExecutionProvider" in ort.get_available_providers() and hasattr(ort, "preload_dlls"):
        ort.preload_dlls()
    from audio_separator.separator import Separator

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_tmp"
    tmp.mkdir(exist_ok=True)
    if not (out_dir / "source.wav").is_file():
        prepare_input(src, out_dir / "source.wav")
    src = out_dir / "source.wav"
    separator = Separator(
        output_dir=str(tmp),
        model_file_dir=str(model_dir),
        output_format="WAV",
        sample_rate=44100,
        use_autocast=True,
        normalization_threshold=1.0,
    )
    timings = {}

    # 每一步的产物都落盘；产物已存在就跳过，换去混响模型时只重跑最后一步
    if not (out_dir / "vocals.wav").is_file():
        t = time.time()
        outputs = _run(separator, VOCAL_MODEL, src, tmp)
        shutil.move(_pick(outputs, tmp, "instrumental"), out_dir / "instrumental.wav")
        shutil.move(_pick(outputs, tmp, "vocals"), out_dir / "vocals.wav")
        timings["vocals"] = round(time.time() - t, 1)

    if not (out_dir / "lead_wet.wav").is_file():
        t = time.time()
        outputs = _run(separator, KARAOKE_MODEL, out_dir / "vocals.wav", tmp)
        shutil.move(_pick(outputs, tmp, "instrumental"), out_dir / "backing.wav")
        shutil.move(_pick(outputs, tmp, "vocals"), out_dir / "lead_wet.wav")
        timings["karaoke"] = round(time.time() - t, 1)

    if not (out_dir / "lead_dry.wav").is_file():
        t = time.time()
        outputs = _run(separator, DEREVERB_MODELS[dereverb], out_dir / "lead_wet.wav", tmp)
        shutil.move(_pick(outputs, tmp, "noreverb", "dry"), out_dir / "lead_dry.wav")
        shutil.move(_pick(outputs, tmp, "reverb", "no dry"), out_dir / "lead_reverb.wav")
        timings["dereverb"] = round(time.time() - t, 1)

    shutil.rmtree(tmp, ignore_errors=True)
    return timings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--dereverb", choices=sorted(DEREVERB_MODELS), default="anvuew")
    args = parser.parse_args()
    timings = separate(args.src, args.out_dir, args.model_dir, args.dereverb)
    print("SEPARATE_DONE " + json.dumps(timings), flush=True)


if __name__ == "__main__":
    sys.exit(main())
