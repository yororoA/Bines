import re
import requests
import os
from pathlib import Path
import sys
import json

# 确保可以从项目根目录导入 config（兼容从不同工作目录运行 speaking 模块）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import VOCU_BASE_URL, VOCU_API_KEY, VOCU_MODEL_ID

def tts_generate_streaming_vocu(text, lang, chunk_callback=None):
    """使用 vocu.ai API 进行流式 TTS 合成，边接收边发送音频块"""
    try:
        # 构建请求头和参数
        headers = {
            "Authorization": f"Bearer {VOCU_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = json.dumps({
          "voiceId": VOCU_MODEL_ID,
          "text": text,
          "promptId": "default",
          "preset": "balance",
          "break_clone": True,
          "language": "auto",
          "vivid": False,
          "emo_switch": [
              0,
              0,
              0,
              0,
              0
          ],
          "speechRate": 1,
          "flash": False,
          "stream": True,
          "seed": -1,
          "srt": False
        })