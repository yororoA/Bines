"""DeepSeek's first-party Search / Fetch APIs, shared by desktop and QQ Skills."""
import time
from urllib.parse import urlsplit
import requests

from config import (
    DEEPSEEK_API_KEY, DEEPSEEK_SEARCH_URL, DEEPSEEK_FETCH_URL, DEEPSEEK_WEB_TIMEOUT,
    DEEPSEEK_SEARCH_COUNT, require_env,
)


def _post(url, payload):
    key = require_env("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
    last_error = "请求失败"
    for attempt in range(3):
        try:
            with requests.post(
                url, json=payload, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=DEEPSEEK_WEB_TIMEOUT, allow_redirects=False,
            ) as response:
                if response.status_code == 200:
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("响应不是 JSON 对象")
                    return data
                status = response.status_code
                last_error = f"HTTP {status}"
                retryable = status in {408, 429} or 500 <= status < 600
                if not retryable:
                    hints = {401: "请检查 .env 中的 DEEPSEEK_API_KEY", 402: "账户余额不足",
                             403: "网页访问被拒绝", 404: "没有可读取的网页内容"}
                    raise RuntimeError(f"DeepSeek 网页接口 {last_error}：{hints.get(status, '请检查请求参数')}")
        except requests.RequestException:
            last_error = "连接失败或超时"
        except ValueError:
            raise RuntimeError("DeepSeek 网页接口返回无效 JSON") from None
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"DeepSeek 网页接口{last_error}，请稍后重试")


def search(query: str, count: int | None = None):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
        raise ValueError("搜索词需为 1–128 个字符")
    count = DEEPSEEK_SEARCH_COUNT if count is None else count
    if type(count) is not int or not 1 <= count <= 50:
        raise ValueError("搜索结果数需为 1–50")
    data = _post(DEEPSEEK_SEARCH_URL, {"query": query.strip(), "count": count, "need_content": False})
    results = data.get("results")
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise RuntimeError("DeepSeek 搜索响应缺少 results")
    # Keep source links/dates alongside model-readable summaries. No private chat
    # context or identity is transmitted beyond the requested query.
    fields = ("title", "url", "snippet", "summary", "site_name", "date", "authority", "rerank_score")
    return {"query": query.strip(), "results": [
        {key: item.get(key) for key in fields} for item in results[:count]
    ]}


def fetch(url: str):
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("网页 URL 需为不超过 8192 字符的 HTTP(S) 地址")
    parsed = urlsplit(url)
    valid_url = parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password
    if not valid_url:
        raise ValueError("网页 URL 必须为 HTTP(S) 地址，不能包含用户名或密码")
    data = _post(DEEPSEEK_FETCH_URL, {"url": url})
    if not isinstance(data.get("content"), str) or not isinstance(data.get("title"), str):
        raise RuntimeError("DeepSeek 网页读取响应缺少 title/content")
    content = data["content"]
    # Model-facing text stays bounded, while memory retains the full received page.
    from tools.context import CURRENT_CONTEXT
    journal = CURRENT_CONTEXT.get().journal
    if journal and len(content) > 24000:
        journal.capture("web_fetch_full", {"url": url},
                        {"url": url, "title": data["title"], "content": content}, category="observation")
    return {"url": url, "title": data["title"], "content": content[:24000], "truncated": len(content) > 24000}
