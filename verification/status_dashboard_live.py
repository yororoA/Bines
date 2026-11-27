"""Isolated live dashboard fixture; no real accounts, model calls or messages.

Run: python -m verification.status_dashboard_live --directory <empty test dir>
Serves the actual manager/memory apps on 15000/15001 and Memory RPC on 15560.
SIGUSR1 stops only the fixture RPC for the offline check. Ctrl+C cleans up servers.
"""
import argparse
import os
from pathlib import Path
import signal
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    for key, value in {
        "BINES_ENV_FILE": os.devnull,
        "BINES_MEMORY_DIR": str(directory), "BINES_MODEL_USAGE_PATH": str(directory / "model_usage.db"),
        "BINES_GATEWAY_DIR": str(directory / "gateway"),
        "BINES_ADMIN_TOKEN": "isolated-dashboard-admin", "BINES_MEMORY_RPC_TOKEN": "isolated-dashboard-memory",
        "BINES_QQ_GATEWAY_TOKEN": "isolated-dashboard-gateway", "BINES_AUTONOMY_BACKEND": "sqlite",
        "BINES_AUTONOMY_SQLITE_PATH": str(directory / "autonomy.db"),
        "BINES_MANAGER_PORT": "15000", "BINES_RAG_WEB_PORT": "15001", "BINES_RAG_RPC_PORT": "15560",
        "BINES_ZMQ_HOST": "127.0.0.1", "BINES_QQ_GATEWAY_URL": "http://127.0.0.1:16102",
        "BINES_MEMORY_BACKEND": "remote", "BINES_OWNER_QQ_IDS": "fixture-owner",
    }.items():
        os.environ[key] = value
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "thingking" / "src"))
    import zmq
    from memory.repository import MemoryRepository
    from memory.service import MemoryService
    from memory.settings import MemorySettings
    from memory.lexical_index import LexicalIndex
    from memory.server import dispatch
    from memory.channels import local_envelope
    from common.model_usage import record_usage
    from common.autonomy_store import AutonomyStore
    from werkzeug.serving import make_server

    repo = MemoryRepository(directory / "memory.db")
    service = MemoryService(repo, LexicalIndex(repo), MemorySettings(data_dir=directory, state_updates=False))
    service.record_turn(local_envelope("测试", timestamp=time.time()).to_dict(), "这是隔离验收用的示例对话", "记忆已保存")
    service.add_memory("semantic", "owner", "测试用户喜欢散步和蓝色")
    service.add_memory("episode", "owner", "今天完成了记忆迁移验证")
    service.put_state("owner", {"mood": "平静", "current_focus": "验证记忆与状态页面"})
    store = AutonomyStore(directory=directory, backend="sqlite")
    store.put("control", {"mode": "pause", "updated_at": time.time()}, 0)
    record_usage({"call_id": "fixture-usage", "kind": "desktop", "model": "验收示例模型",
                  "started_at": time.time(), "status": "completed",
                  "usage": {"prompt_tokens": 1200, "completion_tokens": 240, "prompt_cache_hit_tokens": 600}})
    rpc_stopped = threading.Event()
    socket = zmq.Context.instance().socket(zmq.REP)
    socket.setsockopt(zmq.LINGER, 0)
    socket.bind("tcp://127.0.0.1:15560")

    def rpc_loop():
        try:
            while not rpc_stopped.is_set():
                if socket.poll(100):
                    payload = socket.recv_json()
                    socket.send_json(dispatch(service, payload, token="isolated-dashboard-memory"))
        finally:
            socket.close()

    rpc = threading.Thread(target=rpc_loop, daemon=True)
    rpc.start()
    from server.module_manager import app as manager_app
    from thingking.rag_web_server import app as memory_app
    servers = [make_server("127.0.0.1", port, app, threaded=True)
               for port, app in ((15000, manager_app), (15001, memory_app))]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: rpc_stopped.set())
    print(f"Fixture ready: http://127.0.0.1:15000/status and :15001/status; PID={os.getpid()}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
        rpc_stopped.set()
        rpc.join(timeout=2)
        repo.close()


if __name__ == "__main__":
    main()
