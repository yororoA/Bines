"""Bounded, local-only video decoding in a disposable subprocess."""
from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys

MAX_BYTES = 64 * 1024 * 1024
SAMPLE_FRAMES = 6
MAX_SIDE = 1280
DECODE_TIMEOUT = 30
MAX_PIXELS = 20_000_000
MAX_DECODED_PIXELS = 2_000_000_000


def decode_video(path):
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("视频为空或超过 64 MiB")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "chatBot.video_decode", str(path)],
            capture_output=True, text=True, timeout=DECODE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("视频解码超时") from None
    if result.returncode:
        raise ValueError("视频格式不支持、已损坏或超过解码预算")
    data = json.loads(result.stdout)
    if not data.get("frames") or len(data["frames"]) > SAMPLE_FRAMES:
        raise ValueError("视频没有可读取画面")
    return data


def _format(header):
    # Explicit demuxers prevent playlists from opening network or local files.
    if header[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free"}:
        return "mov"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "matroska"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return "avi"
    if header.startswith(b"FLV"):
        return "flv"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"\x47") and header[188:189] == b"\x47":
        return "mpegts"
    raise ValueError("不支持的容器格式")


def _decode(path):
    import av
    with Path(path).open("rb") as source:
        header = source.read(512)
    with av.open(str(path), format=_format(header),
                 options={"protocol_whitelist": "file", "enable_drefs": "0",
                          "use_absolute_path": "0"}) as container:
        streams = list(container.streams.video)
        if not streams:
            raise ValueError("没有视频轨道")
        stream = streams[0]
        stream.thread_type = "SLICE"
        stream.codec_context.thread_count = 2
        if stream.codec_context.width * stream.codec_context.height > MAX_PIXELS:
            raise ValueError("视频分辨率过大")
        rate = float(stream.average_rate or 25)
        duration = (float(stream.duration * stream.time_base) if stream.duration is not None
                    else float(container.duration or 0) / av.time_base)
        if duration <= 0 and stream.frames:
            duration = stream.frames / rate
        if not 0 < duration <= 3600:
            raise ValueError("无法确定视频时长或时长过长")
        start = float((stream.start_time or 0) * stream.time_base)
        last = max(0, duration - 1 / max(1, rate))
        targets = [last * index / (SAMPLE_FRAMES - 1) for index in range(SAMPLE_FRAMES)]
        images, times, seen = [], [], set()
        decoded_pixels = 0
        rotation = int(float(stream.metadata.get("rotate", "0"))) % 360
        for target in targets:
            container.seek(int((target + start) / stream.time_base), stream=stream,
                           backward=True, any_frame=False)
            chosen, timestamp = None, None
            for index, frame in enumerate(container.decode(stream)):
                decoded_pixels += frame.width * frame.height
                if (index >= 600 or frame.width * frame.height > MAX_PIXELS
                        or decoded_pixels > MAX_DECODED_PIXELS):
                    raise ValueError("视频超过解码预算")
                if frame.time is None:
                    continue
                chosen, timestamp = frame, max(0, float(frame.time) - start)
                if timestamp + .001 >= target:
                    break
            if chosen is None or round(timestamp, 6) in seen:
                continue
            seen.add(round(timestamp, 6))
            image = chosen.to_image()
            display_rotation = getattr(chosen, "rotation", 0) or -rotation
            if display_rotation:
                image = image.rotate(display_rotation, expand=True)
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            output = BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=82)
            images.append("data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode())
            times.append(round(timestamp, 3))
        return {"frames": images, "sample_times": times, "duration_seconds": round(duration, 3),
                "audio_observed": False}


if __name__ == "__main__":
    try:
        # Bound runaway native decoders too; these limits apply only to the child.
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
            if sys.platform.startswith("linux"):
                resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        print(json.dumps(_decode(sys.argv[1]), separators=(",", ":")))
    except Exception:
        raise SystemExit(1)
