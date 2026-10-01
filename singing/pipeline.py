"""翻唱流水线调度：原曲 → 分离（singing venv）→ 换声（so-vits-svc 环境）→ 混音（singing venv）。

本模块只用标准库，可在 Thinking 进程内直接导入；重活都在子进程里跑。
每首歌按文件内容哈希缓存在 SINGING_WORK_DIR/covers/<key>/，同一首歌只处理一次。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import (  # noqa: E402
    SINGING_PYTHON,
    SINGING_SPEAKER,
    SINGING_SVC_DIFFUSION_MODEL,
    SINGING_SVC_MODEL,
    SINGING_WORK_DIR,
    SOVITS_SVC_DIR,
)

# 改动流水线参数或模型时递增，旧缓存自动失效
PIPELINE_VERSION = "1"
AUDIO_SUFFIXES = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mkv", ".webm"}
# 删除后无需保留的中间文件；保留的 instrumental/backing/lead_dry/converted 足够重新混音
_DISPOSABLE = ("source.wav", "vocals.wav", "lead_wet.wav", "lead_reverb.wav")

_job_lock = threading.Lock()

ProgressFn = Callable[[str], None]


class PipelineError(RuntimeError):
    pass


def covers_dir() -> Path:
    return Path(SINGING_WORK_DIR) / "covers"


def song_key(src: Path) -> str:
    digest = hashlib.sha1(PIPELINE_VERSION.encode())
    with open(src, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    digest.update(f"|{SINGING_SVC_MODEL}|{SINGING_SVC_DIFFUSION_MODEL}|{SINGING_SPEAKER}".encode())
    return digest.hexdigest()[:16]


def cached_cover(src: Path) -> Optional[Path]:
    final = covers_dir() / song_key(src) / "final.mp3"
    return final if final.is_file() else None


def check_environment() -> list[str]:
    """返回缺失项说明；空列表表示可以运行。"""
    problems = []
    if not SINGING_PYTHON or not Path(SINGING_PYTHON).is_file():
        problems.append("SINGING_PYTHON 未配置或解释器不存在")
    if not SOVITS_SVC_DIR or not _sovits_python():
        problems.append("SOVITS_SVC_DIR 未配置或找不到 so-vits-svc 自带的 Python")
    elif not (Path(SOVITS_SVC_DIR) / SINGING_SVC_MODEL).is_file():
        problems.append(f"找不到换声模型 {SINGING_SVC_MODEL}")
    return problems


def _sovits_python() -> Optional[Path]:
    base = Path(SOVITS_SVC_DIR) / "python_env"
    for candidate in (base / "Scripts" / "python.exe", base / "Python" / "python.exe", base / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return None


def _env(extra_pythonpath: Optional[Path] = None) -> dict:
    env = dict(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    # 模型推理会写大量临时文件；放到工作目录，避免挤占系统盘
    tmp = Path(SINGING_WORK_DIR) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(tmp), TMP=str(tmp))
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    if extra_pythonpath:
        env["PYTHONPATH"] = str(extra_pythonpath)
    else:
        env.pop("PYTHONPATH", None)
    return env


def _run(step: str, cmd: list, log_path: Path, progress: ProgressFn, env: dict, cwd: Optional[Path] = None) -> dict:
    """运行一步子进程，转发带标记的进度行，返回 *_DONE 行携带的 JSON。"""
    result: dict = {}
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {step}: {' '.join(map(str, cmd))}\n")
        proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        for line in proc.stdout:
            log.write(line)
            marker, _, payload = line.strip().partition(" ")
            if marker.endswith("_DONE"):
                result = json.loads(payload or "{}")
            elif marker == "SEGMENT":
                seg = json.loads(payload)
                if not seg["silence"]:
                    progress(f"{step}: {seg['index'] + 1}/{seg['total']}")
        code = proc.wait()
    if code != 0:
        raise PipelineError(f"{step} 失败（退出码 {code}），详见 {log_path}")
    return result


def prepare_cover(src: Path, progress: ProgressFn = lambda _msg: None) -> Path:
    """生成（或直接返回已缓存的）翻唱成品，阻塞直到完成。同一时间只跑一首。"""
    src = Path(src).resolve()
    problems = check_environment()
    if problems:
        raise PipelineError("；".join(problems))
    with _job_lock:
        key = song_key(src)
        job_dir = covers_dir() / key
        final = job_dir / "final.mp3"
        if final.is_file():
            return final
        job_dir.mkdir(parents=True, exist_ok=True)
        log_path = job_dir / "pipeline.log"
        here = Path(__file__).resolve().parent
        timings = {}
        started = time.time()

        progress("分离人声")
        t = time.time()
        timings["separate_steps"] = _run(
            "separate",
            [SINGING_PYTHON, here / "separate.py", src, job_dir, "--model-dir", Path(SINGING_WORK_DIR) / "models"],
            log_path, progress, _env(),
        )
        timings["separate"] = round(time.time() - t, 1)

        progress("换声")
        t = time.time()
        sovits_dir = Path(SOVITS_SVC_DIR)
        site_packages = sovits_dir / "python_env" / "Lib" / "site-packages"
        _run(
            "convert",
            [_sovits_python(), here / "svc_convert.py", job_dir / "lead_dry.wav", job_dir / "svc",
             "--sovits-dir", sovits_dir, "--model", SINGING_SVC_MODEL,
             "--diffusion-model", SINGING_SVC_DIFFUSION_MODEL, "--speaker", SINGING_SPEAKER],
            log_path, progress, _env(site_packages if site_packages.is_dir() else None), cwd=sovits_dir,
        )
        timings["convert"] = round(time.time() - t, 1)

        progress("混音")
        t = time.time()
        converted = job_dir / "converted.wav"
        shutil.move(str(job_dir / "svc" / "converted.wav"), converted)
        shutil.rmtree(job_dir / "svc", ignore_errors=True)
        partial = job_dir / "final.partial.mp3"
        _run("mix", [SINGING_PYTHON, here / "mix.py", job_dir, converted, partial, "--compact"],
             log_path, progress, _env())
        partial.replace(final)
        timings["mix"] = round(time.time() - t, 1)
        timings["total"] = round(time.time() - started, 1)

        for name in _DISPOSABLE:
            (job_dir / name).unlink(missing_ok=True)
        (job_dir / "meta.json").write_text(json.dumps({
            "source": str(src),
            "title": src.stem,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pipeline_version": PIPELINE_VERSION,
            "timings": timings,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return final


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", type=Path)
    args = parser.parse_args()
    final = prepare_cover(args.src, progress=lambda msg: print(f"[singing] {msg}", flush=True))
    print(final)


if __name__ == "__main__":
    main()
