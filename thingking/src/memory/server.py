"""Memory Server: the single process that owns the SQLite database and the vector index."""
from __future__ import annotations

import json
import logging
import traceback
from typing import Any, Callable, Dict

from .service import MemoryService

logger = logging.getLogger("bines.memory.server")

RPC_METHODS = (
    "ping",
    "record_turn",
    "record_message",
    "record_experience",
    "complete_experience_summary",
    "import_gateway_snapshot",
    "build_context",
    "get_state",
    "state_history",
    "facts",
    "put_state",
    "activity_snapshot",
    "life_snapshot",
    "regulate_life",
    "claim_job",
    "complete_summary",
    "complete_diary",
    "complete_state_update",
    "fail_job",
    "reconcile_index",
    "rebuild_index",
    "stats",
    "list_memories",
    "search",
    "get_memory",
    "add_memory",
    "update_memory",
    "delete_memory",
    "list_conversations",
    "conversation_messages",
    "export",
)


def dispatch(service: MemoryService, request: Dict[str, Any]) -> Dict[str, Any]:
    method = request.get("method")
    params = request.get("params") or {}
    if method not in RPC_METHODS:
        return {"ok": False, "error": f"unknown method: {method}"}
    handler: Callable[..., Any] = getattr(service, method)
    try:
        return {"ok": True, "result": handler(**params)}
    except Exception as exc:
        logger.error("RPC %s failed: %s\n%s", method, exc, traceback.format_exc())
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def build_service() -> MemoryService:
    from .embedding import load_embedder
    from .repository import MemoryRepository
    from .settings import load_settings
    from .vector_index import VectorIndex

    settings = load_settings()
    from chatBot.store import prepare_gateway_database
    prepare_gateway_database(settings.data_dir)
    repo = MemoryRepository(settings.db_path)
    index = VectorIndex(settings.index_dir, load_embedder())
    service = MemoryService(repo, index, settings)
    service.sync_identities()
    result = service.reconcile_index()
    logger.info("memory index reconciled: %s", result)
    return service


def serve(port: int, on_ready: Callable[[], None] | None = None) -> None:
    import zmq

    service = build_service()
    context = zmq.Context.instance()
    socket = context.socket(zmq.REP)
    socket.bind(f"tcp://*:{port}")
    print(f"[Memory Server] listening on tcp://*:{port} ({service.settings.db_path})", flush=True)
    if on_ready:
        on_ready()
    try:
        while True:
            raw = socket.recv()
            try:
                request = json.loads(raw.decode("utf-8"))
            except Exception:
                socket.send_json({"ok": False, "error": "invalid_json"})
                continue
            response = dispatch(service, request)
            socket.send(json.dumps(response, ensure_ascii=False, default=str).encode("utf-8"))
    except KeyboardInterrupt:
        print("[Memory Server] stopping", flush=True)
    finally:
        socket.close(linger=0)
        service.repo.close()
