"""Readiness markers for processes owned by the shared Bines supervisor."""
import json
import os
from pathlib import Path
import time


def managed():
    return bool(os.environ.get("_BINES_SERVICE_TOKEN"))


def mark_ready(name=None, details=None):
    root = os.environ.get("_BINES_RUNTIME_DIR")
    token = os.environ.get("_BINES_SERVICE_TOKEN")
    service = os.environ.get("_BINES_SERVICE_NAME")
    if not root or not token or not service:
        return
    path = Path(root) / f"{service}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "token": token, "pid": os.getpid(), "time": time.time(),
        "name": name or service, "details": details or {},
    }), encoding="utf-8")
    temporary.replace(path)
