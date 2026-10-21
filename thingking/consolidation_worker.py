"""Standalone consolidation service: episode summaries, facts and diaries.

Runs separately from QQ Thinking and desktop Thinking. It talks to the ChatBot
gateway, which forwards memory RPC to either the embedded MemoryService
(chatbot profile) or the RAG Server (full profile).
"""
import logging
import os
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]

from agents import DynamicMemoryMaintainer
from chatBot.memory_hub import GatewayMemoryClient
from common.service_ready import mark_ready
from memory.worker import MemoryWorker, wait_for_server
import persona_profile
from skill_runner import SkillRunner
from thinking_model_helper import ThinkingModelHelper
from tools import TOOLS_REGISTRY
from tools.dependencies import deps

log = logging.getLogger("bines.memory.consolidation")


def build_worker(client, model=None, runner=None):
    """Build the worker with an optional injected model/runner for tests."""
    model = model or ThinkingModelHelper()
    runner = runner or SkillRunner(
        TOOLS_REGISTRY,
        skills_dir=ROOT / "skills",
        enabled_actions_path=ROOT / "server" / "skill_actions.json",
    )
    maintainer = DynamicMemoryMaintainer(runner, client)
    return MemoryWorker(
        client,
        complete_text=lambda messages: model.complete_text(
            messages, temperature=0.3, max_tokens=1500,
        ),
        diary_system_prompt=persona_profile.diary_system_prompt,
        on_owner_summary=maintainer.update_from_summary,
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
