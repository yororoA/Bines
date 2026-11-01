"""Small business-level health probes used by Docker and local diagnostics."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]


def _marker(name):
    installation = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
    directory = Path(os.getenv("BINES_HEALTH_DIR") or Path(tempfile.gettempdir()) / f"bines-health-{installation}")
    return directory / f"{name}.json"


def heartbeat(name, healthy=True):
    path = _marker(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"pid": os.getpid(), "updated": time.time(), "healthy": bool(healthy)}))
        temporary.replace(path)
        return True
    except OSError as exc:
        # A failed diagnostic write must not interrupt an in-flight model job.
        # The previous marker expires, so probes still detect the stale state.
        logging.getLogger("bines.health").warning("%s heartbeat unavailable (%s)", name, type(exc).__name__)
        return False


def _marker_healthy(name, max_age=30):
    try:
        data = json.loads(_marker(name).read_text())
        age = time.time() - float(data["updated"])
        if data.get("healthy") is not True or not 0 <= age <= max_age or int(data["pid"]) <= 0:
            return False
        os.kill(int(data["pid"]), 0)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def check(component):
    if component == "memory":
        from config import ZMQ_PORTS
        from memory.client import MemoryClient
        return MemoryClient("127.0.0.1", ZMQ_PORTS["RAG_SERVER_REQREP"], timeout_ms=3000).ping()
    if component == "memory-worker":
        return _marker_healthy(component)
    if component in {"gateway", "qq-thinking", "gateway-ready", "qq-thinking-ready"}:
        from chatBot.gateway_client import GatewayClient
        from urllib.request import Request
        client = GatewayClient()
        request = Request(client.url + "/health", data=b"{}", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {client.token}"})
        with client.opener.open(request, timeout=4) as response:
            result = json.load(response).get("result")
        if not isinstance(result, dict) or result.get("memory") is not True:
            return False
        readiness = component.endswith("-ready")
        service = component.removesuffix("-ready")
        # Compose readiness describes our own process dependencies. NapCat may
        # legitimately be waiting for a user to scan a login QR code; keep that
        # separate from the original business-health probes below.
        if not readiness and result.get("connected") is not True:
            return False
        transport = result.get("transport", {})
        if not isinstance(transport, dict) or ((readiness or transport)
                and transport.get("event_consumer_alive") is not True):
            return False
        if service == "gateway":
            return True
        queue = result.get("queue", {})
        workers = queue.get("workers_live", 0) if isinstance(queue, dict) else 0
        return type(workers) is int and workers > 0
    if component == "admin":
        from config import HTTP_PORTS
        from common.local_auth import admin_token
        from urllib.request import Request, build_opener, ProxyHandler
        request = Request(f"http://127.0.0.1:{HTTP_PORTS['RAG_MEMORY']}/api/health",
                          headers={"Authorization": f"Bearer {admin_token()}"})
        with build_opener(ProxyHandler({})).open(request, timeout=4) as response:
            return json.load(response).get("success") is True
    raise ValueError("unknown component")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("gateway", "gateway-ready", "memory", "qq-thinking",
                                              "qq-thinking-ready", "memory-worker", "admin"))
    args = parser.parse_args()
    try:
        healthy = check(args.component)
    except Exception as exc:
        print(f"{args.component}: unhealthy ({type(exc).__name__})")
        return 1
    print(f"{args.component}: {'healthy' if healthy else 'unhealthy'}")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
