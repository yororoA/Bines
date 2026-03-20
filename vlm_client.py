from typing import Iterable, Optional, Sequence, Union

import requests


def call_dashscope_vlm(
    image_b64: Union[str, Sequence[str]],
    prompt: str,
    *,
    api_url: str,
    api_key: Optional[str],
    model: str,
    timeout: int,
    proxies: Optional[dict] = None,
    image_labels: Optional[Iterable[str]] = None,
    missing_key_message: str = "",
    request_failed_prefix: str = "[VLM 请求失败",
    empty_message: str = "",
    error_message_prefix: str = "[VLM 异常]",
) -> str:
    """统一 DashScope VLM 请求封装，支持单图/多图。"""
    if not api_key:
        return missing_key_message

    images = [image_b64] if isinstance(image_b64, str) else list(image_b64 or [])
    if not images:
        return empty_message

    normalized = []
    for img in images:
        if not img:
            continue
        if isinstance(img, str) and img.startswith("data:"):
            normalized.append(img)
        else:
            normalized.append(f"data:image/jpeg;base64,{img}")

    content = [{"type": "text", "text": prompt}]
    labels = list(image_labels or [])
    for i, img in enumerate(normalized):
        if i < len(labels) and labels[i]:
            content.append({"type": "text", "text": labels[i]})
        content.append({"type": "image_url", "image_url": {"url": img}})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=timeout,
            proxies=proxies,
        )
        if resp.status_code != 200:
            text = (resp.text or "")[:300]
            return f"{request_failed_prefix} {resp.status_code}] {text}"
        out = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        out = out.strip() if isinstance(out, str) else ""
        return out or empty_message
    except Exception as e:
        return f"{error_message_prefix} {e}" if error_message_prefix else ""
