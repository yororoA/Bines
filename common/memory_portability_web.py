"""Authenticated, read-only memory transfer endpoints shared by both admin apps."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading

from flask import Blueprint, Response, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from thingking.src.memory.settings import load_settings

MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def create_portability_blueprint():
    bp = Blueprint("memory_portability", __name__)
    # Bound expensive disk operations; the existing app admin guard authenticates
    # all these routes before any database or upload is opened.
    operation = threading.BoundedSemaphore(1)

    @bp.get("/api/memory/bundle")
    def download_bundle():
        from thingking.src.memory.portability import export_bundle

        if not operation.acquire(blocking=False):
            return jsonify(success=False, error="正在处理记忆包，请稍后再试"), 409
        temporary = None
        try:
            temporary = tempfile.TemporaryDirectory(prefix="bines-memory-export-")
            archive = Path(temporary.name) / "memory.zip"
            export_bundle(load_settings().db_path, archive)
        except FileNotFoundError:
            if temporary is not None:
                temporary.cleanup()
            return jsonify(success=False, error="当前实例没有记忆库，无法导出"), 404
        except (ValueError, OSError, RuntimeError) as exc:
            if temporary is not None:
                temporary.cleanup()
            return jsonify(success=False, error=f"记忆包导出失败：{exc}"), 503
        except Exception:
            if temporary is not None:
                temporary.cleanup()
            raise
        finally:
            operation.release()

        def stream():
            try:
                with archive.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        yield chunk
            finally:
                temporary.cleanup()

        filename = datetime.now(timezone.utc).strftime("bines-memory-%Y%m%dT%H%M%SZ.zip")
        response = Response(stream(), mimetype="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(archive.stat().st_size),
            "Cache-Control": "no-store",
        })
        response.call_on_close(temporary.cleanup)
        return response

    @bp.post("/api/memory/bundle/inspect")
    def inspect_upload():
        from thingking.src.memory.portability import inspect_bundle

        if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
            return jsonify(success=False, error="网页校验最多支持 512 MiB；更大的记忆包请用 inspect-bundle 命令"), 413
        if not operation.acquire(blocking=False):
            return jsonify(success=False, error="正在处理记忆包，请稍后再试"), 409
        try:
            with tempfile.TemporaryDirectory(prefix="bines-memory-inspect-") as directory:
                archive = Path(directory) / "upload.zip"
                total = 0
                with archive.open("xb") as destination:
                    while chunk := request.stream.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            raise RequestEntityTooLarge()
                        destination.write(chunk)
                result = inspect_bundle(archive)
                return jsonify(success=True, result=result)
        except RequestEntityTooLarge:
            return jsonify(success=False, error="记忆包超过网页校验的 512 MiB 限制"), 413
        except (ValueError, OSError, RuntimeError) as exc:
            return jsonify(success=False, error=f"记忆包校验失败：{exc}"), 400
        finally:
            operation.release()

    return bp
