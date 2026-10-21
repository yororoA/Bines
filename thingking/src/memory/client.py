"""Typed RPC client for the Memory Server. Safe to share across threads (one REQ socket per call)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("bines.memory.client")


class MemoryUnavailable(RuntimeError):
    pass


class MemoryClient:
    def __init__(self, host: str, port: int, context=None, timeout_ms: int = 30000):
        import zmq

        self._zmq = zmq
        self._context = context or zmq.Context.instance()
        self._endpoint = f"tcp://{host}:{port}"
        self.timeout_ms = timeout_ms

    def call(self, method: str, timeout_ms: Optional[int] = None, **params: Any) -> Any:
        zmq = self._zmq
        socket = self._context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVTIMEO, timeout_ms or self.timeout_ms)
        socket.setsockopt(zmq.SNDTIMEO, 5000)
        try:
            socket.connect(self._endpoint)
            socket.send(json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8"))
            reply = json.loads(socket.recv().decode("utf-8"))
        except zmq.error.Again as exc:
            raise MemoryUnavailable(f"Memory Server timed out on {method} ({self._endpoint})") from exc
        except zmq.ZMQError as exc:
            raise MemoryUnavailable(f"Memory Server error on {method}: {exc}") from exc
        finally:
            socket.close()
        if not reply.get("ok"):
            raise RuntimeError(f"Memory Server {method} failed: {reply.get('error')}")
        return reply.get("result")

    def ping(self, timeout_ms: int = 3000) -> bool:
        try:
            self.call("ping", timeout_ms=timeout_ms)
            return True
        except Exception:
            return False

    # ---------- conversation ----------
    def record_turn(self, envelope: Dict[str, Any], user_text: str = "", assistant_text: str = "") -> Dict[str, Any]:
        return self.call("record_turn", envelope=envelope, user_text=user_text, assistant_text=assistant_text)

    def record_message(self, envelope: Dict[str, Any], role: str, content: str) -> Dict[str, Any]:
        return self.call("record_message", envelope=envelope, role=role, content=content)

    def build_context(self, envelope: Dict[str, Any], query: str = "") -> Dict[str, Any]:
        return self.call("build_context", envelope=envelope, query=query)

    def get_state(self, scope: str = "owner") -> Dict[str, Any]:
        return self.call("get_state", scope=scope)

    def put_state(self, scope: str, data: Dict[str, Any], expected_version: Optional[int] = None) -> Dict[str, Any]:
        return self.call("put_state", scope=scope, data=data, expected_version=expected_version)

    def activity_snapshot(self, limit: int = 10) -> Dict[str, Any]:
        return self.call("activity_snapshot", limit=limit, timeout_ms=5000)

    # ---------- consolidation ----------
    def claim_job(self) -> Optional[Dict[str, Any]]:
        return self.call("claim_job")

    def complete_summary(self, job_id: int, summary: str, tags: Sequence[str], facts: Sequence[Dict[str, Any]],
                         importance: float) -> Dict[str, Any]:
        return self.call("complete_summary", job_id=job_id, summary=summary, tags=list(tags), facts=list(facts),
                         importance=importance)

    def complete_diary(self, job_id: int, text: str) -> Dict[str, Any]:
        return self.call("complete_diary", job_id=job_id, text=text)

    def fail_job(self, job_id: int, error: str) -> Dict[str, Any]:
        return self.call("fail_job", job_id=job_id, error=error)

    # ---------- admin ----------
    def stats(self) -> Dict[str, Any]:
        return self.call("stats")

    def list_memories(self, **filters: Any) -> Dict[str, Any]:
        return self.call("list_memories", **filters)

    def search(self, query: str, scopes: Sequence[str], kinds: Optional[Sequence[str]] = None, k: int = 10) -> List[Dict[str, Any]]:
        return self.call("search", query=query, scopes=list(scopes), kinds=list(kinds) if kinds else None, k=k)

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        return self.call("get_memory", memory_id=memory_id)

    def add_memory(self, kind: str, scope: str, content: str, **fields: Any) -> Dict[str, Any]:
        return self.call("add_memory", kind=kind, scope=scope, content=content, **fields)

    def update_memory(self, memory_id: str, **fields: Any) -> Dict[str, Any]:
        return self.call("update_memory", memory_id=memory_id, **fields)

    def delete_memory(self, memory_id: str) -> Dict[str, Any]:
        return self.call("delete_memory", memory_id=memory_id)

    def list_conversations(self) -> List[Dict[str, Any]]:
        return self.call("list_conversations")

    def conversation_messages(self, conversation_id: str, offset: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        return self.call("conversation_messages", conversation_id=conversation_id, offset=offset, limit=limit)

    def export(self, scopes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        return self.call("export", scopes=list(scopes) if scopes else None, timeout_ms=120000)

    def rebuild_index(self) -> Dict[str, Any]:
        return self.call("rebuild_index", timeout_ms=600000)
