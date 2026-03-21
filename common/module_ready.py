# -*- coding: utf-8 -*-
"""
模块就绪通知：除 Thinking 外各模块彻底启动后向 Classification 发送一次，
Classification 收到所有模块就绪后再通知 Thinking 正式启动（上线通知、摘要/日记等）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import zmq
from config import ZMQ_HOST, ZMQ_PORTS
from common.zmq_rpc import zmq_req_json


def notify_module_ready(module_name: str) -> None:
    """
    向 Classification 上报本模块已彻底启动。
    调用一次即可；Classification 收集齐所有模块后会通知 Thinking 正式启动。
    """
    port = ZMQ_PORTS.get("MODULE_READY_REP")
    if port is None:
        print(f"[{module_name}] MODULE_READY_REP 未配置，跳过就绪上报", flush=True)
        return
    ctx = zmq.Context.instance()
    try:
        rep = zmq_req_json(
            ctx,
            f"tcp://{ZMQ_HOST}:{port}",
            {"action": "module_ready", "module": module_name},
            recv_timeout_ms=5000,
            send_timeout_ms=2000,
            linger_ms=2000,
        )
        if rep.get("ok"):
            print(f"[{module_name}] 已向 Classification 上报就绪", flush=True)
        else:
            print(f"[{module_name}] 就绪上报响应异常: {rep}", flush=True)
    except Exception as e:
        print(f"[{module_name}] 就绪上报失败: {e}", flush=True)
