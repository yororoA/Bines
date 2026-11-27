#!/usr/bin/env python3
"""Exercise the real on-demand image path with an isolated recorded capture source.

Only pyautogui.size/screenshot are substituted. DPI handling, resize, JPEG,
focused prompt, production vision client and usage accounting remain real.
This does not verify desktop screen permissions. A real model call requires
the explicit --run-model flag and is limited to one attempt / 600 output tokens.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run-model", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ["BINES_MODEL_USAGE_PATH"] = str((args.out / "model-usage.sqlite").resolve())
    from PIL import Image
    from common.model_usage import capture_model_usage
    from tools import screen_tool
    from vlm_client import call_vision
    source = Image.open(args.image).convert("RGB")
    fake_capture = SimpleNamespace(size=lambda: (1280, 720), screenshot=lambda: source.copy())
    sent, usage = {}, []

    def bounded_vision(encoded, prompt, **kwargs):
        data = base64.b64decode(encoded)
        (args.out / "on-demand-detail.jpg").write_bytes(data)
        with Image.open(io.BytesIO(data)) as image:
            sent.update(width=image.width, height=image.height, imageSha256=hashlib.sha256(data).hexdigest(), bytes=len(data), prompt=prompt)
        if not args.run_model:
            return "[Image path only; no model request was made]"
        return call_vision(encoded, prompt, **{**kwargs, "max_tokens": 600, "retries": 1, "timeout": 60})

    focus = "请逐字读取配置详情中的版本号、校验码、重试间隔、备注和发布批次；无法确定的字符写看不清，不要猜测。只报告这五项。"
    started = time.time()
    with patch.dict(sys.modules, {"pyautogui": fake_capture}), patch.object(screen_tool, "call_vision", bounded_vision), capture_model_usage(usage.append):
        response = screen_tool.get_screen_info(simple_recognition=True, focus_description=focus, fast_mode=False)
    result = {"captureSource": "recorded controlled browser PNG; pyautogui capture substituted", "sourceImage": str(args.image), "sourceImageSha256": hashlib.sha256(args.image.read_bytes()).hexdigest(), "logicalSize": [1280, 720], "sourcePhysicalSize": list(source.size), "productionTool": "tools.screen_tool.get_screen_info(simple_recognition=True, focus_description=..., fast_mode=False)", "productionToolSha256": hashlib.sha256((ROOT / "tools/screen_tool.py").read_bytes()).hexdigest(), "modelCalled": args.run_model, "sent": sent, "elapsedSeconds": time.time() - started, "response": response, "usageEvents": [e for e in usage if e.get("status") != "started"]}
    (args.out / "on-demand-detail.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"size": [sent.get("width"), sent.get("height")], "response": response, "usage": [e.get("usage") for e in result["usageEvents"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
