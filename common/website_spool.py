"""Signed file-spool transport for Docker-to-host website bridge calls."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import uuid


MAX_SPOOL_BYTES = 1024 * 1024


def acquire_bridge_lock(path):
    """Hold this descriptor until shutdown; a crashed process releases the lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("网站浏览器桥已经运行，请使用现有 Bines 启动会话") from exc
    return handle


def _canonical(kind, fields):
    return json.dumps(
        {"kind": kind, **fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _signature(token, kind, fields):
    return hmac.new(token.encode(), _canonical(kind, fields), hashlib.sha256).hexdigest()


def signed_envelope(token, kind, fields):
    fields = dict(fields)
    return {**fields, "signature": _signature(token, kind, fields)}


def verify_envelope(token, kind, envelope):
    if not isinstance(envelope, dict):
        return False
    fields = {key: value for key, value in envelope.items() if key != "signature"}
    return hmac.compare_digest(
        str(envelope.get("signature") or ""),
        _signature(token, kind, fields),
    )


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_SPOOL_BYTES:
        raise ValueError("网站桥请求或响应超过 1 MiB")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_SPOOL_BYTES:
        raise ValueError("网站桥文件超过 1 MiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("网站桥文件必须是 JSON 对象")
    return value


def heartbeat(token):
    return signed_envelope(token, "heartbeat", {"ts": time.time()})
