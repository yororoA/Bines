"""独立记忆服务：后台更新会话状态、摘要、长期事实和日记。

QQ 与桌面共用；通过 ChatBot 网关访问 Memory Server。
"""
import logging
import os
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]

from chatBot.memory_hub import GatewayMemoryClient
from common.service_ready import mark_ready
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
            messages, temperature=0.3, max_tokens=8192,
        ),
        summary_complete_text=lambda messages: model.complete_text(
            messages, temperature=0.3, max_tokens=8192,
            response_format={"type": "json_object"},
        ),
        diary_system_prompt=persona_profile.diary_system_prompt,
        state_complete_text=lambda messages: model.complete_text(
            messages, temperature=0.0, max_tokens=4096,
            response_format={"type": "json_object"},
        ),
        idle_seconds=2.0,
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = GatewayMemoryClient()
    timeout = float(os.environ.get("BINES_CONSOLIDATION_WAIT", "180"))
    if not wait_for_server(client, timeout_seconds=timeout):
        raise SystemExit("记忆网关未就绪；请先启动 ChatBot 网关")
    deps.register_memory_client(client)
    worker = build_worker(client)
    worker.start()
    mark_ready("Memory Worker", {"transport": "gateway"})
    log.info("Memory Worker ready")

    stop = threading.Event()

    def handle_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        while not stop.wait(1):
            if not worker.alive:
                raise RuntimeError("Memory Worker 线程已退出")
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        log.info("Memory Worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
