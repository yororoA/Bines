"""Bounded, local-only video decoding and See Now sheets in a disposable child."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from common.video_file import (MAX_BYTES, MAX_DECODED_PIXELS, MAX_PIXELS,
                               MAX_SIDE, VideoFile, container_format as _format)

SAMPLE_FPS = 4.0
MAX_SAMPLES = 240
MAX_STORYBOARDS = 8
DECODE_TIMEOUT = 45
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def decode_video(path):
    path = Path(path).resolve()
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("视频为空或超过 64 MiB")
    kwargs = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen(
        [sys.executable, "-m", "chatBot.video_decode", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(Path(__file__).resolve().parents[1]), **kwargs,
    )
    try:
        stdout, _ = process.communicate(timeout=DECODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            # Include the Node child: a timed-out decoder must not leave a worker.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                # Killing Python still closes Node's stdin; do not skip cleanup
                # if taskkill is unavailable in a minimal Windows environment.
                pass
        process.kill()
        process.communicate()
        raise ValueError("视频解码超时") from None
    if process.returncode or len(stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("视频格式不支持、依赖不可用、已损坏或超过解码预算")
    try:
        data = json.loads(stdout)
        frames, sheets = data["frames"], data["storyboards"]
        if not 0 < len(frames) == len(sheets) <= MAX_STORYBOARDS:
            raise ValueError("视频没有可读取画面")
        for image, sheet in zip(frames, sheets):
            if (not isinstance(image, str) or not image.startswith("data:image/png;base64,")
                    or sheet.get("layout") != "grid3"
                    or not 0 < len(sheet["frames"]) == len(sheet["cells"]) <= 9):
                raise ValueError("视频拼图协议无效")
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("视频拼图协议无效") from None
    return data


def _decode(path):
    from common.video_storyboard import sheet_data_url
    original_as = None
    if sys.platform.startswith("linux"):
        import resource
        original_as = resource.getrlimit(resource.RLIMIT_AS)

    def limit_decoder_memory():
        if original_as is not None:
            soft = 2 * 1024**3
            if original_as[0] != resource.RLIM_INFINITY:
                soft = min(soft, original_as[0])
            resource.setrlimit(resource.RLIMIT_AS, (soft, original_as[1]))

    # Parse untrusted headers under the same limit as actual frame decoding.
    limit_decoder_memory()
    with VideoFile(path, sample_fps=SAMPLE_FPS, max_samples=MAX_SAMPLES,
                   before_decode=limit_decoder_memory) as source:
        if original_as is not None:
            # Node/V8 needs its own address space. iter_storyboards starts Node
            # before requesting frames; frames() re-applies the decoder limit
            # before the first container.decode call. No decoding happens here.
            resource.setrlimit(resource.RLIMIT_AS, original_as)
        images, metadata = [], []
        for sheet in source.storyboards(max_storyboards=MAX_STORYBOARDS):
            images.append(sheet_data_url(sheet))
            metadata.append({key: value for key, value in sheet.items() if key != "png"})
        if not images:
            raise ValueError("视频没有可读取画面")
        # Compatibility: `frames` remains the list of model images. Its entries
        # are now sheets; callers must use storyboards, not label them as frames.
        return {**source.metadata, "frames": images, "storyboards": metadata,
                "image_kind": "storyboards", "layout": "grid3"}


if __name__ == "__main__":
    try:
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (35, 35))
        print(json.dumps(_decode(sys.argv[1]), separators=(",", ":")))
    except Exception:
        raise SystemExit(1)
