"""Memory Server: the single process that owns the SQLite database and the vector index."""
from __future__ import annotations

import json
import logging
import traceback
from typing import Any, Callable, Dict

from .service import MemoryService
from common.local_auth import token_matches

logger = logging.getLogger("bines.memory.server")

RPC_METHODS = (
    "ping",
    "record_turn",
    "record_message",
    "record_experience",
    "complete_experience_summary",
    "complete_memory_repair",
    "complete_memory_audit",
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
    "complete_association",
    "complete_summary",
    "complete_diary",
    "complete_state_update",
    "fail_job",
    "record_model_call",
    "reconcile_index",
    "rebuild_index",
    "stats",
    "list_memories",
    "list_association_nodes",
    "memory_detail",
    "correct_memory",
    "memory_graph",
    "list_jobs",
    "search",
    "search_with_trace",
    "read_memory_source",
    "get_memory",
    "add_memory",
    "update_memory",
    "delete_memory",
    "list_conversations",
    "conversation_messages",
    "export",
)


def dispatch(service: MemoryService, request: Dict[str, Any], *, token: str | None = None) -> Dict[str, Any]:
    """Dispatch trusted in-process calls; network callers must supply the server token."""
    if not isinstance(request, dict):
        return {"ok": False, "error": "invalid_request"}
    if token is not None:
        supplied = request.get("token")
        if not token_matches(supplied, token):
            return {"ok": False, "error": "authentication required"}
    method = request.get("method")
    params = request.get("params", {})
    if not isinstance(params, dict) or not isinstance(method, str):
        return {"ok": False, "error": "invalid_request"}
    if method not in RPC_METHODS:
        return {"ok": False, "error": f"unknown method: {method}"}
    try:
        handler: Callable[..., Any] = getattr(service, method)
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
    from .vector_maintenance import VectorMaintenance
    repo.vector_maintenance = VectorMaintenance(service)
    service.sync_identities()
    logger.info("memory vector maintenance queued: %s", repo.vector_maintenance.status())
    return service


def serve(port: int, on_ready: Callable[[], None] | None = None, *, host: str | None = None,
          token: str | None = None, stop_event=None) -> None:
    import zmq
    from config import MEMORY_BIND_HOST
    from common.local_auth import memory_token

    host = host if host is not None else MEMORY_BIND_HOST
    token = token if token is not None else memory_token()
    if not token:
        raise ValueError("Memory Server requires a non-empty credential")
    service = build_service()
    context = zmq.Context.instance()
    socket = context.socket(zmq.REP)
    socket.setsockopt(zmq.MAXMSGSIZE, 16 * 1024 * 1024)
    try:
        socket.bind(f"tcp://{host}:{port}")
        print(f"[Memory Server] listening on tcp://{host}:{port}", flush=True)
        if on_ready:
            on_ready()
        while stop_event is None or not stop_event.is_set():
            maintenance = getattr(service.repo, "vector_maintenance", None)
            if maintenance is not None:
                maintenance.tick()
            if not socket.poll(50 if maintenance is not None else 500):
                continue
            raw = socket.recv()
            multipart = socket.getsockopt(zmq.RCVMORE)
            while socket.getsockopt(zmq.RCVMORE):
                socket.recv()  # Drain before replying, without retaining extra frames.
            if multipart:
                socket.send_json({"ok": False, "error": "invalid_multipart_request"})
                continue
            if len(raw) > 16 * 1024 * 1024:
                socket.send_json({"ok": False, "error": "request_too_large"})
                continue
            try:
                request = json.loads(raw.decode("utf-8"))
            except Exception:
                socket.send_json({"ok": False, "error": "invalid_json"})
                continue
            response = dispatch(service, request, token=token)
            socket.send(json.dumps(response, ensure_ascii=True, default=str).encode("utf-8"))
    except KeyboardInterrupt:
        print("[Memory Server] stopping", flush=True)
    finally:
        socket.close(linger=0)
        maintenance = getattr(service.repo, "vector_maintenance", None)
        if maintenance is not None:
            maintenance.close()
        service.repo.close()
