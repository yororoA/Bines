#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Memory Server process entry (registered as "RAG Server" in process_registry).

Owns ``memory_data/memory.db`` (SQLite source of truth) and ``memory_data/index`` (Chroma, rebuildable).
"""
import logging
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CURRENT_DIR)
SRC_DIR = os.path.join(CURRENT_DIR, "src")
for path in (ROOT_DIR, SRC_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from config import ZMQ_PORTS  # noqa: E402
from memory.server import serve  # noqa: E402


def _notify_ready() -> None:
    from common.service_ready import mark_ready
    mark_ready("RAG Server")
    try:
        from common.module_ready import notify_module_ready

        notify_module_ready("RAG Server")
    except Exception as exc:
        print(f"[Memory Server] 就绪上报失败: {exc}", flush=True)


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    serve(ZMQ_PORTS["RAG_SERVER_REQREP"], on_ready=_notify_ready)
