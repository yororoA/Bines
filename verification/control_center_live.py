"""Isolated live control center fixture; no real accounts, model calls or messages.

Run: python -m verification.control_center_live --directory <empty test dir>
Serves the actual manager/memory apps on 15000/15001 and Memory RPC on 15560.
SIGUSR1 stops only the fixture RPC for the offline check. Ctrl+C cleans up servers.
"""
import argparse
import os
from pathlib import Path
import signal
import sys
import json
import shutil
from unittest.mock import patch
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    root = Path(__file__).resolve().parents[1]
    for name in (".env.example", "thingking/persona/persona.yaml", "server/skill_actions.json"):
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    (project / ".env").write_text("# Isolated GUI acceptance settings\nBINES_OWNER_NAME=验收主人\nDEEPSEEK_API_KEY=isolated-secret-value\n", encoding="utf-8")
    (project / "mcp.servers.json").write_text(json.dumps({"mcpServers": {"fixture": {"command": "fixture-only", "args": [], "env": {"API_TOKEN": "isolated-mcp-secret"}}}}))
    (project / "server/realtime_screen_config.json").write_text('{"enabled": false, "interval_sec": 10}')
    for key, value in {
        "BINES_ENV_FILE": os.devnull, "BINES_CONFIG_ROOT": str(project),
        "BINES_BUS_PUBLISH_PORT": "15550", "BINES_BUS_SUBSCRIBE_PORT": "15551",
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
    service.put_state("owner", {"current_location": "验收房间", "npc_activity": "验证设置与状态页面"})
    service.put_state("test:second", {"current_location": "第二个范围", "mood": "平静"})
    repo.conn.execute("INSERT INTO jobs(kind,scope,status,attempts,last_error,payload,created_at,updated_at,available_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      ("associate", "owner", "failed", 1, "ConnectionError: isolated test", '{"last_failure":{"causes":["ConnectionError"]}}', time.time(), time.time(), time.time()))
    repo.conn.commit()
    store = AutonomyStore(directory=directory, backend="sqlite")
    store.put("control", {"mode": "pause", "updated_at": time.time()}, 0)
    store.put("desktop", {"activity": {"mode": "review"}, "intention_queue": [], "actions_today": 1}, 0)
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
    # The real supervisor controls only a harmless fixture subprocess. All
    # persistent config paths and system-command ports are isolated.
    definitions = [{"name": "隔离测试服务", "command": [sys.executable, "-c", "from common.service_ready import mark_ready; import time; mark_ready(); time.sleep(3600)"],
                    "cwd": str(root), "ports": [], "depends_on": []}]
    with patch("process_registry.get_processes", return_value=definitions):
        import server.module_manager as manager
    manager.PRESENCE_STATE_PATH = directory / "presence_state.json"
    manager.REALTIME_SCREEN_CONFIG_PATH = project / "server/realtime_screen_config.json"
    manager.SKILL_ACTIONS_PATH = project / "server/skill_actions.json"
    manager.REALTIME_SCREEN_LOG_PATH = directory / "screen.log"
    manager_app = manager.app
    from thingking.rag_web_server import app as memory_app
    servers = [make_server("127.0.0.1", port, app, threaded=True)
               for port, app in ((15000, manager_app), (15001, memory_app))]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: rpc_stopped.set())
    print(f"Fixture ready: http://127.0.0.1:15000/control and :15001/control; PID={os.getpid()}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
        rpc_stopped.set()
        rpc.join(timeout=2)
        manager.supervisor.stop()
        repo.close()


if __name__ == "__main__":
    main()
