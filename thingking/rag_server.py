"""Compatibility memory RPC for full desktop mode; SQLite lives in both processes."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]
from rag_memory import RAGMemory
from config import ZMQ_HOST, ZMQ_PORTS


class RAGServer:
    def __init__(self):
        self.memory = RAGMemory()

    def handle_request(self, request):
        method = request.get("method", "")
        allowed = {
            "get_relevant_context", "get_stats", "collection_get", "collection_count",
            "collection_delete", "similarity_search_with_score", "add_raw_conversation",
            "add_episode_summary", "add_to_summary_buffer", "add_to_diary",
            "update_memory", "garbage_collect",
        }
        if method not in allowed:
            return {"ok": False, "error": "unsupported memory method"}
        try:
            return {"ok": True, "result": getattr(self.memory, method)(**request.get("params", {}))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def main():
    import zmq
    from common.module_ready import notify_module_ready
    server = RAGServer()
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind(f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['RAG_SERVER_REQREP']}")
    notify_module_ready("RAG Server")
    print("共享 SQLite 记忆 RPC 已就绪", flush=True)
    try:
        while True:
            socket.send_json(server.handle_request(socket.recv_json()))
    except KeyboardInterrupt:
        pass
    finally:
        socket.close(linger=0)
        context.term()


if __name__ == "__main__":
    main()
