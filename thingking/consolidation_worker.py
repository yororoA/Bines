"""独立记忆服务：后台更新会话状态、摘要、长期事实和日记。

QQ 与桌面共用；通过 ChatBot 网关访问 Memory Server。
"""
import logging
import argparse
import os
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]

from chatBot.memory_hub import GatewayMemoryClient
from common.service_ready import mark_ready
from common.healthcheck import heartbeat
from memory.worker import MemoryWorker, wait_for_server
import persona_profile
from thinking_model_helper import ThinkingModelHelper
from tools.dependencies import deps

log = logging.getLogger("bines.memory.consolidation")


def build_worker(client, model=None):
    """构建记忆后台任务，可注入模型以验证完整链路。"""
    model = model or ThinkingModelHelper()
    return MemoryWorker(
        client,
        complete_text=lambda messages: model.complete_text(
            messages, temperature=0.3, max_tokens=8192, timeout=120, max_retries=0,
        ),
        summary_complete_text=lambda messages: model.complete_text(
            messages, temperature=0.3, max_tokens=8192, timeout=120, max_retries=0,
            response_format={"type": "json_object"},
        ),
        association_complete_text=lambda messages: model.complete_text(
            messages, temperature=0.1, max_tokens=4096, retry_tokens=8192,
            thinking="disabled", timeout=90, max_retries=0,
            response_format={"type": "json_object"},
        ),
        diary_system_prompt=persona_profile.diary_system_prompt,
        state_complete_text=lambda messages: model.complete_text(
            messages, temperature=0.0, max_tokens=4096, retry_tokens=8192, timeout=90, max_retries=0,
            response_format={"type": "json_object"},
        ),
        idle_seconds=2.0,
        association_concurrency=int(os.environ.get("BINES_MEMORY_ASSOCIATION_CONCURRENCY", "4")),
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct-memory-server", action="store_true",
                        help="直接连接本机 Memory Server；无需启动 QQ 网关即可整理记忆")
    args = parser.parse_args()
    if args.direct_memory_server:
        from config import ZMQ_PORTS
        from memory.client import MemoryClient
        client = MemoryClient("127.0.0.1", ZMQ_PORTS["RAG_SERVER_REQREP"])
    else:
        client = GatewayMemoryClient()
    timeout = float(os.environ.get("BINES_CONSOLIDATION_WAIT", "180"))
    if not wait_for_server(client, timeout_seconds=timeout):
        raise SystemExit("记忆服务未就绪；请先启动 Memory Server 或 ChatBot 网关")
    deps.register_memory_client(client)
    worker = build_worker(client)
    worker.start()
    heartbeat("memory-worker", worker.alive)
    mark_ready("Memory Worker", {"transport": "direct" if args.direct_memory_server else "gateway"})
    log.info("Memory Worker ready")

    stop = threading.Event()

    def handle_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        while not stop.wait(5):
            heartbeat("memory-worker", worker.alive)
            if not worker.alive:
                raise RuntimeError("Memory Worker 线程已退出")
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        heartbeat("memory-worker", False)
        log.info("Memory Worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
