"""Local-only PyAV decoding, bounded sampling and streaming video storyboards.

The same reader serves trusted local files and the disposable QQ decoder. It
never fetches a URL or runs a model. Sampling spans the file, not just its start;
limits and the actually processed interval remain explicit in ``metadata``.
"""
from contextlib import closing
import math
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_DECODED_PIXELS = 2_000_000_000
MAX_SIDE = 1280


def container_format(header):
    # Explicit demuxers plus disabled references reject playlists and remote IO.
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


class VideoFile:
    """An explicitly closed reader; only one resized RGB frame is yielded at a time."""

    def __init__(self, path, *, sample_fps=4.0, max_samples=240,
                 max_bytes=MAX_BYTES, max_duration=3600,
                 max_decoded_pixels=MAX_DECODED_PIXELS, before_decode=None):
        if (isinstance(sample_fps, bool) or not isinstance(sample_fps, (int, float))
                or not math.isfinite(sample_fps) or not 0 < sample_fps <= 30):
            raise ValueError("采样帧率必须大于 0 且不超过 30")
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or not 2 <= max_samples <= 100000:
            raise ValueError("采样预算必须为 2 至 100000")
        for value in (max_bytes, max_duration, max_decoded_pixels):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("视频读取预算必须为有限正数")
        self.path = Path(path)
        self.sample_fps = float(sample_fps)
        self.max_samples, self.max_bytes = max_samples, max_bytes
        self.max_duration, self.max_decoded_pixels = max_duration, max_decoded_pixels
        self.before_decode = before_decode
        self.container = None
        self.metadata = {}
        self._used = False

    def __enter__(self):
        import av
        if not self.path.is_file() or not 0 < self.path.stat().st_size <= self.max_bytes:
            raise ValueError("视频为空或超过文件大小预算")
        with self.path.open("rb") as source:
            format_name = container_format(source.read(512))
        self.container = av.open(str(self.path), format=format_name,
                                 options={"protocol_whitelist": "file", "enable_drefs": "0",
                                          "use_absolute_path": "0"})
        try:
            streams = list(self.container.streams.video)
            if not streams:
                raise ValueError("没有视频轨道")
            self.stream = streams[0]
            self.stream.thread_type = "SLICE"
            self.stream.codec_context.thread_count = 2
            if self.stream.codec_context.width * self.stream.codec_context.height > MAX_PIXELS:
                raise ValueError("视频分辨率过大")
            self.rate = float(self.stream.average_rate or 25)
            if not math.isfinite(self.rate) or self.rate <= 0:
                raise ValueError("视频帧率无效")
            duration = (float(self.stream.duration * self.stream.time_base)
                        if self.stream.duration is not None else float(self.container.duration or 0) / av.time_base)
            if duration <= 0 and self.stream.frames:
                duration = self.stream.frames / self.rate
            if not math.isfinite(duration) or not 0 < duration <= self.max_duration:
                raise ValueError("无法确定视频时长或时长超过预算")
            self.start = float((self.stream.start_time or 0) * self.stream.time_base)
            last = max(0.0, duration - 1 / self.rate)
            requested = max(1, math.ceil(last * min(self.sample_fps, self.rate)) + 1)
            count = min(self.max_samples, requested)
            self.targets = [last * i / (count - 1) for i in range(count)] if count > 1 else [0.0]
            effective_fps = (count - 1) / last if last > 0 and count > 1 else min(self.sample_fps, self.rate)
            self.metadata = {
                "duration_seconds": round(duration, 3), "audio_observed": False,
                "requested_sample_fps": self.sample_fps,
                "effective_sample_fps": effective_fps,
                "sample_budget": self.max_samples, "planned_samples": count,
                "sampling_reduced": requested > self.max_samples,
                "sample_times": [], "sampling_complete": False,
                "decoded_pixels": 0, "decode_budget_reached": False, "processed_from_seconds": None,
                "processed_until_seconds": None,
            }
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.container is not None:
            self.container.close()
            self.container = None

    def __exit__(self, *_):
        self.close()

    def frames(self):
        if self.container is None or self._used:
            raise RuntimeError("视频读取器未打开或已被消费")
        self._used = True
        if self.before_decode is not None:
            self.before_decode()
        rotation = int(float(self.stream.metadata.get("rotate", "0"))) % 360
        decoder, previous = None, -1.0
        for target in self.targets:
            # Dense samples reuse a sequential decoder. Sparse samples seek, so
            # an hour-long input does not force us to decode every intervening frame.
            if decoder is None or target - previous > 2:
                self.container.seek(int((target + self.start) / self.stream.time_base),
                                    stream=self.stream, backward=True, any_frame=False)
                decoder = iter(self.container.decode(self.stream))
            chosen, timestamp = None, None
            for index, frame in enumerate(decoder):
                pixels = frame.width * frame.height
                self.metadata["decoded_pixels"] += pixels
                if pixels > MAX_PIXELS:
                    raise ValueError("视频超过解码预算")
                if index >= 600 or self.metadata["decoded_pixels"] > self.max_decoded_pixels:
                    if not self.metadata["sample_times"]:
                        raise ValueError("视频超过解码预算")
                    self.metadata["decode_budget_reached"] = True
                    return  # The finite pipeline drains any already selected frames.
                if frame.time is None:
                    continue
                timestamp = max(0.0, float(frame.time) - self.start)
                if timestamp <= previous + 1e-6:
                    continue
                chosen = frame
                if timestamp + .001 >= target:
                    break
            if chosen is None:
                continue
            previous = timestamp
            image = chosen.to_image()
            display_rotation = getattr(chosen, "rotation", 0) or -rotation
            if display_rotation:
                image = image.rotate(display_rotation, expand=True)
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            image = image.convert("RGB")
            self.metadata["sample_times"].append(round(timestamp, 6))
            if self.metadata["processed_from_seconds"] is None:
                self.metadata["processed_from_seconds"] = round(timestamp, 6)
            self.metadata["processed_until_seconds"] = round(timestamp, 6)
            yield timestamp, image
        self.metadata["sampling_complete"] = bool(self.metadata["sample_times"]) and (
            self.metadata["sample_times"][-1] >= self.targets[-1] - max(.05, 1 / self.rate))

    def storyboards(self, *, max_storyboards=8, width=1260, height=720, options=None):
        """Stream selected sheets; reaching the sheet cap is explicitly partial.

        The upstream generator is closed even when the consumer stops early.
        Source samples are never collected into an in-memory full-video list.
        """
        from common.video_storyboard import iter_storyboards
        if isinstance(max_storyboards, bool) or not isinstance(max_storyboards, int) or not 1 <= max_storyboards <= 10000:
            raise ValueError("拼图预算必须为 1 至 10000")
        self.metadata.update(storyboard_budget=max_storyboards, storyboard_count=0,
                             storyboard_limit_reached=False, selected_times=[])
        fps = min(30.0, max(.2, self.metadata["effective_sample_fps"]))
        with closing(self.frames()) as frames, closing(iter_storyboards(
                frames, width=width, height=height, fps=fps, options=options)) as sheets:
            for sheet in sheets:
                if self.metadata["storyboard_count"] >= max_storyboards:
                    self.metadata["storyboard_limit_reached"] = True
                    break
                self.metadata["storyboard_count"] += 1
                self.metadata["selected_times"].extend(frame["t"] for frame in sheet["frames"])
                yield sheet
        selected = self.metadata["selected_times"]
        self.metadata["selected_from_seconds"] = min(selected) if selected else None
        self.metadata["selected_until_seconds"] = max(selected) if selected else None
        self.metadata["coverage_note"] = coverage_note(self.metadata)


def coverage_note(metadata):
    notes = ["仅分析间隔采样的画面，不保证捕获瞬时动作；未分析声音"]
    if metadata.get("sampling_reduced"):
        notes.append(f"采样预算 {metadata['sample_budget']} 帧，已降低采样密度至约 {metadata['effective_sample_fps']:.3g} fps")
    if metadata.get("storyboard_limit_reached"):
        end = metadata.get("selected_until_seconds")
        notes.append(f"已达到 {metadata['storyboard_budget']} 张拼图预算，仅分析至约 {end:g} 秒，后续画面未分析" if end is not None else "已达到拼图预算，后续画面未分析")
    elif metadata.get("decode_budget_reached"):
        end = metadata.get("processed_until_seconds", 0)
        notes.append(f"解码预算已用完，最后采样时间为 {end:g} 秒，后续画面未分析")
    elif not metadata.get("sampling_complete"):
        notes.append("视频采样未完成，不代表完整时段")
    return "；".join(notes)
