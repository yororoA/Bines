#!/usr/bin/env python3
"""Screen capture → See Now storyboard → Bines vision → timestamped observation.

The existing realtime-screen switch controls this worker. Sampling is local;
only selected storyboards are sent through the configured Bines vision client.
"""
from __future__ import annotations

import argparse
from logging.handlers import RotatingFileHandler
import logging
import os
from pathlib import Path
import signal
import sys
import threading

from realtime_screen_config import load_realtime_screen_config, save_realtime_screen_config

PROJECT_ROOT = Path(__file__).resolve().parent
SERVER_DIR = PROJECT_ROOT / "server"
CONFIG_PATH = SERVER_DIR / "realtime_screen_config.json"
PID_PATH = SERVER_DIR / "realtime_screen_pid.txt"
CONTEXT_PATH = SERVER_DIR / "realtime_screen_context.json"
OBSERVATION_PATH = SERVER_DIR / "realtime_screen_observation.json"
FLUSH_SIGNAL_PATH = SERVER_DIR / "realtime_screen_flush.signal"
LOG_PATH = SERVER_DIR / "realtime_screen_analysis.log"


def capture_screen_resized(scale=1.0):
    """Capture the primary monitor; no images are written to disk."""
    import mss
    from PIL import Image
    with mss.mss() as screen:
        monitor = screen.monitors[1] if len(screen.monitors) > 1 else screen.monitors[0]
        grabbed = screen.grab(monitor)
        image = Image.frombytes("RGB", grabbed.size, grabbed.bgra, "raw", "BGRX")
    if scale != 1:
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                             Image.Resampling.LANCZOS)
    return image


def call_vision(image, prompt):
    from vlm_client import call_vision as describe
    return describe(image, prompt, max_tokens=1000, timeout=45,
                    image_labels=["屏幕采样拼图（时间与位置见提示）"],
                    missing_key_message="[VLM 未配置]", empty_message="[VLM 无描述]",
                    error_message_prefix="[VLM 异常]", proxies={"http": None, "https": None})


def run_loop(stop=None):
    from common.screen_runtime import ScreenWorker
    from common.service_ready import mark_ready
    ScreenWorker(config_path=CONFIG_PATH, context_path=CONTEXT_PATH,
                 flush_path=FLUSH_SIGNAL_PATH, observation_path=OBSERVATION_PATH,
                 capture=capture_screen_resized, vision=call_vision,
                 ready=lambda: mark_ready("Realtime Screen"), stop=stop).run()


def main(argv=None):
    from common.env import load_env
    load_env()
    parser = argparse.ArgumentParser(description="实时屏幕采样、See Now 筛帧拼图和视觉分析")
    parser.add_argument("--interval", "-i", type=int, help="两次自动视觉请求的最小间隔（3–120秒）")
    parser.add_argument("--check", action="store_true", help="只检查运行依赖，不截图、不调用模型")
    args = parser.parse_args(argv)
    if args.check:
        import mss  # noqa: F401
        from PIL import Image  # noqa: F401
        from common.see_now import find_node_binary
        print("See Now runtime ready:", find_node_binary())
        return 0
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(
                            LOG_PATH, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")])
    if args.interval is not None:
        config = load_realtime_screen_config(CONFIG_PATH)
        config["interval_sec"] = args.interval
        save_realtime_screen_config(CONFIG_PATH, config)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    try:
        run_loop(stop)
        return 0
    except Exception as exc:
        # Never log screenshots, prompts, descriptions, signed URLs or credentials.
        logging.error("Screen worker stopped (%s); check Node, screen permission and configuration", type(exc).__name__)
        return 1
    finally:
        try:
            if PID_PATH.read_text(encoding="ascii").strip() == str(os.getpid()):
                PID_PATH.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
