"""Shared, same-origin status dashboard for the host manager and memory Web UI."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler

from flask import Blueprint, jsonify, render_template

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 1.5
PROBE_DEADLINE = 5.0
CACHE_SECONDS = 5.0


def _gateway_request(path, payload):
    from chatBot.config import GATEWAY_URL, gateway_token
    req = Request(GATEWAY_URL.rstrip("/") + path, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + gateway_token(),
    })
    with build_opener(ProxyHandler({})).open(req, timeout=TIMEOUT) as response:
        result = json.load(response).get("result")
    if not isinstance(result, dict):
        raise ValueError("invalid status response")
    return result


def _memory():
    from config import ZMQ_HOST, ZMQ_PORTS
    source = str(ROOT / "thingking" / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from memory.client import MemoryClient
    backend = os.getenv("BINES_MEMORY_BACKEND", "auto").lower()
    local = ZMQ_HOST.strip().lower() in {"127.0.0.1", "localhost", "::1"}
    client = MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"], timeout_ms=int(TIMEOUT * 1000))
    call = client.call
    if backend == "embedded":
        call = lambda method, **params: _gateway_request("/memory", {"method": method, "params": params})
    try:
        stats = call("stats")
    except Exception:
        if backend != "auto" or not local:
            raise
        call = lambda method, **params: _gateway_request("/memory", {"method": method, "params": params})
        stats = call("stats")
    if not isinstance(stats, dict):
        raise ValueError("invalid memory statistics")
    # Do not forward identity links, conversations or raw backend errors.
    allowed = ("memories", "messages", "jobs", "unindexed", "index_count", "index_unit",
               "association_nodes", "association_edges", "association_ready", "nodes_by_status",
               "jobs_by_kind", "retention_enabled", "associations_enabled", "life", "model_usage_24h")
    result = {key: stats[key] for key in allowed if key in stats}
    vector = stats.get("vector_status") or {}
    result["vector_status"] = {key: vector[key] for key in (
        "ready", "building", "running", "queued", "degraded", "pending_keys", "quarantined_keys", "failures", "kind"
    ) if key in vector}
    try:
        result["state"] = call("get_state", scope="owner")
    except Exception:
        result["state_unavailable"] = True
    return result


def _gateway():
    result = _gateway_request("/health", {})
    return {key: result[key] for key in ("connected", "memory", "queue", "transport") if key in result}


def _autonomy():
    """Read existing state without creating a database or seeding legacy values."""
    from common.autonomy_store import memory_directory
    backend = os.getenv("BINES_AUTONOMY_BACKEND", "redis").lower()
    if backend == "sqlite":
        path = Path(os.getenv("BINES_AUTONOMY_SQLITE_PATH") or memory_directory() / "autonomy.db")
        if not path.exists():
            return {"configured": False, "backend": backend}
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=TIMEOUT) as db:
            row = db.execute("SELECT value FROM documents WHERE key='control'").fetchone()
            control = json.loads(row[0]) if row else {}
    elif backend == "redis":
        from common.autonomy_store import AutonomyStore
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        store = AutonomyStore()
        # Keep dashboard probes shorter than a normal scheduler operation.
        store.redis.connection_pool.connection_kwargs.update(
            socket_timeout=TIMEOUT, socket_connect_timeout=TIMEOUT,
            retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
        try:
            row = store.redis.hgetall(store.prefix + "control")
            control = json.loads(row["value"]) if row else {}
        finally:
            store.redis.close()
    else:
        raise ValueError("unsupported autonomy backend")
    return {"configured": True, "backend": backend, "control": control.get("mode", "auto"),
            "updated_at": control.get("updated_at")}


def _services(supervisor):
    if supervisor is None:
        return {"managed": False, "items": []}
    rows = []
    for name in supervisor.definitions:
        status = supervisor.status(name)
        rows.append({"name": name, **{key: status.get(key) for key in ("state", "running", "pid", "start_time")}})
    return {"managed": True, "items": rows}


def _probe(callback):
    try:
        data = callback()
        status = "unknown" if data.get("configured") is False or data.get("managed") is False else "online"
        return {"status": status, "checked_at": time.time(), "data": data}
    except Exception as exc:
        # Connection refusal/timeouts prove unavailability. Application/config errors
        # are unknown; never imply that a process was stopped or reveal exception text.
        from urllib.error import HTTPError, URLError
        unavailable = (isinstance(exc, (ConnectionError, TimeoutError, URLError)) and not isinstance(exc, HTTPError)) or type(exc).__name__ in {
            "MemoryUnavailable", "ConnectionError", "TimeoutError"}
        return {"status": "offline" if unavailable else "unknown", "checked_at": time.time(),
                "error": "暂时无法连接，稍后自动重试" if unavailable else "状态暂不可读取，请检查服务配置", "data": None}


def create_status_blueprint(supervisor=None, ports=None):
    server = ROOT / "server"
    blueprint = Blueprint("status_dashboard", __name__, template_folder=str(server / "templates"),
                          static_folder=str(server / "dashboard_static"), static_url_path="/status-assets")
    cache, lock = {}, threading.Lock()
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bines-status")
    futures = {}

    @blueprint.get("/status")
    def status_page():
        return render_template("status_dashboard.html", ports=ports or {})

    @blueprint.get("/api/status/overview")
    def overview():
        # Coalesce tabs/pollers. A refresh never starts competing probe batches.
        with lock:
            if not cache or time.monotonic() - cache["time"] >= CACHE_SECONDS:
                callbacks = {"memory": _memory, "gateway": _gateway, "autonomy": _autonomy,
                             "services": lambda: _services(supervisor)}
                for key, callback in callbacks.items():
                    # A stalled probe keeps its one slot; polling must not queue
                    # new copies of it or accumulate worker threads indefinitely.
                    if key not in futures or futures[key].done():
                        futures[key] = pool.submit(_probe, callback)
                wait(futures.values(), timeout=PROBE_DEADLINE)
                result = {key: future.result() if future.done() else {
                    "status": "unknown", "checked_at": time.time(), "data": None,
                    "error": "本次状态探测超时，其他组件仍可查看",
                } for key, future in futures.items()}
                cache.update(time=time.monotonic(), result={"updated_at": time.time(), "components": result})
            response = jsonify(success=True, result=cache["result"])
        response.headers["Cache-Control"] = "no-store"
        return response

    return blueprint
