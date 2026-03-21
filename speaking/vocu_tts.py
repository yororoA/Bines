import requests
from path_setup import ensure_project_root

# 确保可以从项目根目录导入 config（兼容从不同工作目录运行 speaking 模块）
PROJECT_ROOT = ensure_project_root(__file__, 1)

from config import VOCU_BASE_URL, VOCU_API_KEY, VOCU_MODEL_ID

def _build_vocu_payload(text: str, lang: str) -> dict:
    """构建 vocu TTS 请求体。"""
    language = "auto"
    if lang in {"zh", "ja", "en"}:
        language = lang

    return {
        "voiceId": VOCU_MODEL_ID,
        "text": text,
        "promptId": "default",
        "preset": "balance",
        "break_clone": True,
        "language": language,
        "vivid": False,
        "emo_switch": [0, 0, 0, 0, 0],
        "speechRate": 1,
        "flash": False,
        "stream": True,
        "seed": -1,
        "srt": False,
    }


def _iter_vocu_urls():
    """兼容不同网关部署，按顺序尝试。"""
    base = (VOCU_BASE_URL or "").rstrip("/")
    if not base:
        return
    yield f"{base}/v1/tts"
    yield f"{base}/tts"


def tts_generate_streaming_vocu(text, lang, chunk_callback=None):
    """使用 vocu.ai API 进行流式 TTS 合成，边接收边发送音频块。"""
    if not text:
        print("❌ VOCU 文本为空")
        return None

    if not VOCU_API_KEY:
        print("❌ VOCU_API_KEY 未配置")
        return None

    headers = {
        "Authorization": f"Bearer {VOCU_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = _build_vocu_payload(str(text), str(lang or "auto"))

    last_err = None
    for url in _iter_vocu_urls():
        try:
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=120,
                stream=True,
            )
            if resp.status_code == 404:
                last_err = f"404: {url}"
                continue
            resp.raise_for_status()

            audio_chunks = []
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                audio_chunks.append(chunk)
                if chunk_callback:
                    try:
                        chunk_callback(chunk)
                    except Exception as cb_err:
                        print(f"⚠️ VOCU chunk_callback 执行失败: {cb_err}")

            audio_data = b"".join(audio_chunks)
            if not audio_data:
                print("⚠️ VOCU 返回空音频")
                return None
            return audio_data

        except Exception as e:
            last_err = e
            continue

    print(f"❌ VOCU 流式合成失败: {last_err}")
    return None