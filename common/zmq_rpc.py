from typing import Any, Dict, Optional

import zmq


def zmq_req_json(
    context: zmq.Context,
    endpoint: str,
    payload: Dict[str, Any],
    *,
    recv_timeout_ms: int = 5000,
    send_timeout_ms: int = 2000,
    linger_ms: int = 2000,
) -> Dict[str, Any]:
    """一次性 REQ->JSON RPC。调用方负责捕获异常。"""
    req = context.socket(zmq.REQ)
    req.setsockopt(zmq.LINGER, linger_ms)
    req.setsockopt(zmq.RCVTIMEO, recv_timeout_ms)
    req.setsockopt(zmq.SNDTIMEO, send_timeout_ms)
    try:
        req.connect(endpoint)
        req.send_json(payload)
        reply = req.recv_json()
        return reply if isinstance(reply, dict) else {"ok": False, "error": "invalid reply"}
    finally:
        try:
            req.close()
        except Exception:
            pass


def zmq_req_string(
    context: zmq.Context,
    endpoint: str,
    payload: str,
    *,
    recv_timeout_ms: int = 5000,
    send_timeout_ms: int = 2000,
    linger_ms: int = 2000,
) -> Optional[str]:
    """一次性 REQ->STRING RPC。调用方负责捕获异常。"""
    req = context.socket(zmq.REQ)
    req.setsockopt(zmq.LINGER, linger_ms)
    req.setsockopt(zmq.RCVTIMEO, recv_timeout_ms)
    req.setsockopt(zmq.SNDTIMEO, send_timeout_ms)
    try:
        req.connect(endpoint)
        req.send_string(payload)
        return req.recv_string()
    finally:
        try:
            req.close()
        except Exception:
            pass
