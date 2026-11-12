"""Resolve video bytes only from the current QQ event or its real reply."""
import base64
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit

from chatBot.messages import segments
from chatBot.quotes import message_id, validate_quote_peer
from chatBot.video_decode import MAX_BYTES, decode_video


class VideoService:
    def __init__(self, service):
        self.service = service

    def _authorize(self, identity, event_id, mid):
        if not event_id or identity.local:
            raise PermissionError("视频读取需要当前 QQ 消息")
        with self.service.store.connection() as db:
            row = db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()
        event = json.loads(row["raw"]) if row else {}
        if event.get("post_type") != "message" or self.service.store.identity(event) != identity:
            raise PermissionError("视频不属于当前 QQ 会话")
        current_id = str(event.get("message_id") or "")
        references = {
            str((part.get("data") or {}).get("id"))
            for part in segments(event.get("message", event.get("raw_message", "")))
            if part.get("type") == "reply"
        }
        if mid != current_id and mid not in references:
            raise PermissionError("只能读取本轮消息及其实际引用的视频")
        # Both source and referring messages must remain present and unrecalled.
        self.service._message(identity, current_id)
        self.service._message(identity, mid)
        return event

    def call(self, identity, method, params, *, event_id=None):
        if method not in {"frames", "validate"}:
            raise ValueError("未知视频读取方法")
        if not isinstance(params, dict) or set(params) - {"message_id", "video_index"}:
            raise ValueError("视频参数只能指定消息编号和视频序号")
        mid = message_id(params.get("message_id"))
        index = params.get("video_index", 0)
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 2:
            raise ValueError("视频序号必须为 0 或 1")
        event = self._authorize(identity, event_id, mid)
        if method == "validate":
            return {"status": "ok"}
        row = self.service._message(identity, mid)
        from chatBot.quotes import cached_quote
        source = event if mid == str(event.get("message_id")) else cached_quote(identity, mid, row)
        original_parts = [part for part in segments(
            (source or {}).get("message", (source or {}).get("raw_message", ""))
        ) if part.get("type") == "video"]
        if index >= len(original_parts):
            raise PermissionError("该消息没有对应的视频段")
        # get_msg refreshes expiring CDN URLs, but never changes the authorized peer.
        result = self.service.client.raw_api("get_msg", {"message_id": mid}, timeout=10)
        if isinstance(result, dict) and result.get("status") == "ok":
            fresh = result.get("data") or {}
            validate_quote_peer(fresh, identity, mid, cached=True)
            fresh_parts = [part for part in segments(fresh.get("message", fresh.get("raw_message", "")))
                           if part.get("type") == "video"]
            if index >= len(fresh_parts):
                raise ValueError("视频已不可读取")
            original_parts = fresh_parts
        self._authorize(identity, event_id, mid)
        data = self._bytes(original_parts[index].get("data") or {})
        self._authorize(identity, event_id, mid)
        with tempfile.TemporaryDirectory(prefix="bines-qq-video-") as directory:
            path = Path(directory) / "source.video"
            path.write_bytes(data)
            frames = decode_video(path)
        self._authorize(identity, event_id, mid)
        return {"status": "ok", "message_id": mid, "video_index": index, **frames}

    def _bytes(self, data, *, resolve_file=True):
        candidates = [data.get(key) for key in ("url", "base64", "file", "path") if data.get(key)]
        for value in candidates:
            if not isinstance(value, str):
                continue
            if value.startswith(("https://", "http://")):
                from chatBot.stickers import download_image
                try:
                    return download_image(value, max_bytes=MAX_BYTES, timeout=30,
                                          accept="video/*,application/octet-stream")
                except (ValueError, OSError, TimeoutError):
                    continue
            encoded = None
            if value.startswith("base64://"):
                encoded = value.removeprefix("base64://")
            elif value.startswith("data:video/") and ";base64," in value:
                encoded = value.split(";base64,", 1)[1]
            elif value == data.get("base64"):
                encoded = value
            if encoded is not None:
                if len(encoded) > (MAX_BYTES + 2) // 3 * 4:
                    raise ValueError("视频超过 64 MiB")
                result = base64.b64decode(encoded, validate=True)
                if not 0 < len(result) <= MAX_BYTES:
                    raise ValueError("视频为空或过大")
                return result
            local = self._local_path(value)
            if local and local.is_file():
                with local.open("rb") as stream:
                    result = stream.read(MAX_BYTES + 1)
                if not 0 < len(result) <= MAX_BYTES:
                    raise ValueError("视频为空或超过 64 MiB")
                return result
        if resolve_file:
            file_id = data.get("file_id") or data.get("file")
            # Only a native opaque file identifier; never ask NapCat to fetch a
            # model-provided URL or arbitrary filesystem path.
            if isinstance(file_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", file_id):
                result = self.service.client.raw_api("get_file", {"file_id": file_id}, timeout=15)
                if isinstance(result, dict) and result.get("status") == "ok":
                    return self._bytes(result.get("data") or {}, resolve_file=False)
        raise ValueError("视频地址已过期、不可访问或来源不受支持")

    @staticmethod
    def _local_path(value):
        from chatBot.config import ROOT
        root = Path(ROOT) / "deploy" / "napcat" / "data"
        if value.startswith("file://"):
            parsed = urlsplit(value)
            if parsed.netloc not in {"", "localhost"}:
                return None
            value = unquote(parsed.path)
        for prefix, relative in (("/app/.config/QQ/", "qq"), ("/bines-media/", "media")):
            if value.startswith(prefix):
                value = str(root / relative / value[len(prefix):])
                break
        path = Path(value)
        if not path.is_absolute():
            return None
        path = path.resolve()
        return path if path.is_relative_to(root.resolve()) else None
