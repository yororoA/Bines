import time
import base64
import binascii
import json
from typing import Iterable, Optional, Sequence, Union

import requests


def normalize_image(image: str) -> str:
    """Accept a public URL, data URL or OneBot/raw base64; preserve the actual format."""
    if not isinstance(image, str):
        raise ValueError("图片输入必须是 URL 或 base64 字符串")
    if image.startswith(("http://", "https://")):
        if len(image) > 8192:
            raise ValueError("图片 URL 超过 8192 字符")
        return image
    encoded = image.removeprefix("base64://")
    if image.startswith("data:"):
        header, separator, encoded = image.partition(",")
        if not separator or ";base64" not in header:
            raise ValueError("图片 data URL 必须使用 base64")
    if len(encoded) > 45 * 1024 * 1024:
        raise ValueError("图片超过 DeepSeek 单图 32 MiB 限制")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("图片 base64 无效") from None
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("图片超过 DeepSeek 单图 32 MiB 限制")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("仅支持 JPEG、PNG、GIF 和 WebP 图片")
    return f"data:{mime};base64,{encoded}"


def call_vision(
    image_b64: Union[str, Sequence[str]],
    prompt: str,
    *,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    proxies: Optional[dict] = None,
    image_labels: Optional[Iterable[str]] = None,
    missing_key_message: str = "",
    request_failed_prefix: str = "[VLM 请求失败",
    empty_message: str = "",
    error_message_prefix: str = "[VLM 异常]",
    max_tokens: Optional[int] = None,
    retries: int = 1,
) -> str:
    """DeepSeek Flash vision; one credential for QQ, desktop, camera and video."""
    from config import (
        DEEPSEEK_API_KEY, DEEPSEEK_VISION_API_URL, DEEPSEEK_VISION_MODEL,
        DEEPSEEK_VISION_TIMEOUT, DEEPSEEK_VISION_DETAIL,
    )
    api_url = DEEPSEEK_VISION_API_URL if api_url is None else api_url
    api_key = DEEPSEEK_API_KEY if api_key is None else api_key
    model = DEEPSEEK_VISION_MODEL if model is None else model
    timeout = DEEPSEEK_VISION_TIMEOUT if timeout is None else timeout
    if not api_key:
        return missing_key_message

    images = [image_b64] if isinstance(image_b64, str) else list(image_b64 or [])
    if not images:
        return empty_message

    if len(images) > 600:
        return f"{request_failed_prefix} 图片数量超过 600]"
    try:
        normalized = [normalize_image(img) for img in images if img]
    except ValueError as exc:
        return f"{request_failed_prefix} {exc}]"
    if not normalized:
        return empty_message

    content = [{"type": "text", "text": prompt}]
    labels = list(image_labels or [])
    for i, img in enumerate(normalized):
        if i < len(labels) and labels[i]:
            content.append({"type": "text", "text": labels[i]})
        content.append({"type": "image_url", "image_url": {"url": img, "detail": DEEPSEEK_VISION_DETAIL}})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        # Vision descriptions need text within the requested token budget.
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if len(json.dumps(payload).encode("utf-8")) > 48 * 1024 * 1024:
        return f"{request_failed_prefix} 请求体超过 DeepSeek 48 MiB 限制，请减少图片]"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    attempts = min(3, max(1, int(retries)))
    last_error = ""
    for attempt in range(attempts):
        try:
            with requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=timeout,
                proxies=proxies,
                allow_redirects=False,
            ) as resp:
                if resp.status_code != 200:
                    last_error = f"DeepSeek HTTP {resp.status_code}"
                    if resp.status_code not in {408, 429} and resp.status_code < 500:
                        break
                else:
                    choice = (resp.json().get("choices") or [{}])[0]
                    out = choice.get("message", {}).get("content", "")
                    out = out.strip() if isinstance(out, str) else ""
                    if choice.get("finish_reason") == "length" and not out:
                        return f"{request_failed_prefix} token 预算不足，未得到图片描述]"
                    return out or empty_message
        except (requests.RequestException, ValueError) as exc:
            # Never include raw transport errors that may contain signed image URLs.
            last_error = f"DeepSeek {type(exc).__name__}"
        if attempt + 1 < attempts:
            time.sleep(min(0.25 * (attempt + 1), 1.0))

    if request_failed_prefix:
        return f"{request_failed_prefix} {last_error}]"
    return f"{error_message_prefix} {last_error}" if error_message_prefix else ""


# Compatibility for extensions importing the previous helper name.
# In-repository callers use call_vision; no automatic fallback to DashScope.
call_dashscope_vlm = call_vision
