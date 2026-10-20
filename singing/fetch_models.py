"""预下载 separate.py 用到的分离模型。

audio-separator 默认从 GitHub Releases 下载权重，在国内经常卡死；这里改为
从 Hugging Face（可用 HF_ENDPOINT 指向镜像）取权重，GitHub 只取很小的 yaml 配置。
文件已存在时跳过，audio-separator 运行时检测到本地文件便不会再联网下载。
"""
import argparse
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.env import load_env

load_env()

from separate import DEREVERB_MODELS, KARAOKE_MODEL, VOCAL_MODEL

HF_REPO = "Politrees/UVR_resources"
HF_DIRS = {
    VOCAL_MODEL: "models/Roformer/BandSplit",
    KARAOKE_MODEL: "models/Roformer/MelBand",
    **{name: "models/Roformer/MelBand" for name in DEREVERB_MODELS.values()},
}
YAML_URL_PREFIXES = (
    "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/mdx_model_data/mdx_c_configs",
    "https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs",
)


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done, reported = 0, -10
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
            done += len(chunk)
            percent = done * 100 // total if total else 0
            if percent >= reported + 10:
                reported = percent
                print(f"  {dest.name}: {percent}%", flush=True)
    tmp.replace(dest)


def _required_files(model_dir: Path) -> dict:
    import logging
    from audio_separator.separator import Separator

    separator = Separator(model_file_dir=str(model_dir), log_level=logging.WARNING, info_only=True)
    wanted = set(HF_DIRS)
    files = {}
    for models in separator.list_supported_model_files().values():
        for info in models.values():
            if info["filename"] in wanted:
                files[info["filename"]] = info["download_files"]
    return files


def fetch(model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
    for ckpt, download_files in _required_files(model_dir).items():
        for name in download_files:
            dest = model_dir / name
            if dest.is_file() and dest.stat().st_size > 0:
                continue
            if name.endswith(".yaml"):
                for prefix in YAML_URL_PREFIXES:
                    try:
                        _download(f"{prefix}/{name}", dest)
                        break
                    except OSError:
                        continue
                else:
                    raise RuntimeError(f"无法下载配置 {name}")
            else:
                _download(f"{endpoint}/{HF_REPO}/resolve/main/{HF_DIRS[ckpt]}/{name}", dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    fetch(parser.parse_args().model_dir)
    print("FETCH_DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
