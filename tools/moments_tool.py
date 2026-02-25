"""
动态（Moments）后端 API 客户端。
与 Express 提供的 /api/moments 接口字段与路径一致，供 Thinking / 工具模型调用。
uid 与 token 从环境变量 MOMENTS_UID、MOMENTS_TOKEN 读取，请求头必带。
调用动态相关工具时写入 moments_tools.log，记录工具名、参数及结果摘要。
"""
import os
import json
from pathlib import Path
from typing import Optional, Any
from datetime import datetime
import sys

try:
    import requests
except ImportError:
    requests = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    MOMENTS_UID,
    MOMENTS_TOKEN,
    MOMENTS_API_BASE_URL,
    MOMENTS_API_TIMEOUT,
)


# 动态工具调用日志：与本模块同目录（tools/moments_tools.log）
_MOMENTS_LOG_PATH: Path = Path(__file__).resolve().parent / "moments_tools.log"


def _log_moments_call(tool_name: str, params: dict, result: Any) -> None:
    """将本次调用的工具名、参数、结果摘要写入 moments_tools.log。"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params_str = json.dumps(params, ensure_ascii=False)
        if isinstance(result, dict):
            msg = result.get("message", "")
            data = result.get("data")
            if data is None:
                summary = msg or "data=null"
            else:
                summary = f"ok, data_type={type(data).__name__}"
        else:
            summary = str(result)[:80]
        line = f"{ts} | {tool_name} | {params_str} | {summary}\n"
        with open(_MOMENTS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _url(path: str) -> str:
    base = (os.environ.get("MOMENTS_API_BASE_URL") or MOMENTS_API_BASE_URL).rstrip("/")
    p = path if path.startswith("/") else f"/{path}"
    return f"{base}/api/moments{p}"


def _headers() -> dict:
    """鉴权：Authorization Bearer + uid，从环境变量读取。"""
    h = {"Content-Type": "application/json", "uid": MOMENTS_UID or ""}
    if MOMENTS_TOKEN:
        h["Authorization"] = f"Bearer {MOMENTS_TOKEN}"
    return h


def _check_requests():
    if requests is None:
        raise RuntimeError("moments 模块依赖 requests，请安装: pip install requests")


def get_moments() -> dict:
    """
    获取已发布动态列表（isEditing 固定为 false，与前端 getMoments 一致）。uid/token 从环境变量读取。
    返回: { "message": "ok", "data": 动态列表或 null }
    """
    _check_requests()
    params = {"isEditing": "false"}
    try:
        r = requests.get(
            _url("/get"),
            params=params,
            headers=_headers(),
            timeout=MOMENTS_API_TIMEOUT,
        )
        r.raise_for_status()
        out = r.json()
        _log_moments_call("get_moments", params, out)
        return out
    except Exception as e:
        _log_moments_call("get_moments", params, f"Error: {e}")
        raise


def _simplify_moment_item(item: dict) -> dict:
    """
    将后端返回的单条 moment 文档，规整为更适合大模型解析的一致结构。
    - 只保留常用字段
    - 将 filenames / filesDetail 展平为 images 数组
    - 统一换行符为 \\n
    """
    filenames = item.get("filenames") or {}
    files_detail = item.get("filesDetail") or {}
    images = []
    for fname, url in filenames.items():
        detail = files_detail.get(fname) or {}
        images.append(
            {
                "url": url,
                "filename": fname,
                "origin": detail.get("origin"),
                "desc": detail.get("desc") or "",
            }
        )

    return {
        "id": item.get("_id") or item.get("documentId"),
        "title": item.get("title") or "",
        "content": (item.get("content") or "").replace("\r\n", "\n"),
        "username": item.get("username"),
        "createdAt": item.get("createdAt"),
        "likes": item.get("likes", 0),
        "views": item.get("views", 0),
        "comments_count": len(item.get("comments") or []),
        "acknowledge": item.get("acknowledge", False),
        "images": images,
    }


def get_moments_simple() -> dict:
    """
    面向大模型的简化版 get_moments。

    返回:
    {
        "message": "ok",
        "data": [
            {
                "id": "...",
                "title": "...",
                "content": "...",   # 已替换为 \\n 换行
                "username": "...",
                "createdAt": "...",
                "likes": 0,
                "views": 0,
                "comments_count": 0,
                "acknowledge": true/false,
                "images": [
                    {
                        "url": "...",
                        "filename": "...",
                        "origin": "...",
                        "desc": "..."
                    },
                    ...
                ]
            },
            ...
        ]
    }
    """
    params = {"isEditing": "false", "mode": "simple"}
    raw = get_moments()
    data = raw.get("data") or []
    simplified = []
    for m in data:
        if isinstance(m, dict):
            simplified.append(_simplify_moment_item(m))
    out = {"message": raw.get("message", ""), "data": simplified}
    _log_moments_call("get_moments_simple", params, out)
    return out


def add_moment(title: str, content: str = "") -> dict:
    """
    发布动态。published 固定 true，acknowledge 固定 false，无文件/描述，uid/token 从环境变量读取。
    返回: { "message": "ok", "data": 创建的 moment 文档 }
    """
    _check_requests()
    if not title:
        out = {"message": "缺少必要字段：title", "data": None}
        _log_moments_call("add_moment", {"title": title, "content": content or ""}, out)
        return out
    payload = {
        "title": title,
        "content": content or "",
        "published": "true",
        "acknowledge": "false",
    }
    params = {"title": title, "content": content or ""}
    try:
        r = requests.post(
            _url("/post"),
            json=payload,
            headers=_headers(),
            timeout=MOMENTS_API_TIMEOUT,
        )
        r.raise_for_status()
        out = r.json()
        _log_moments_call("add_moment", params, out)
        return out
    except Exception as e:
        _log_moments_call("add_moment", params, f"Error: {e}")
        raise


def comment_moment(moment_id: str, comment: str, belong: Optional[str] = None) -> dict:
    """
    对某条动态发表评论。uid/token 从环境变量读取。
    返回: { "message": "ok", "data": 新评论文档 }
    """
    _check_requests()
    body = {"momentId": moment_id, "comment": comment}
    if belong is not None and str(belong).strip():
        body["belong"] = str(belong).strip()
    params = {"moment_id": moment_id, "comment": comment, "belong": belong}
    try:
        r = requests.post(
            _url("/comment/post"),
            json=body,
            headers=_headers(),
            timeout=MOMENTS_API_TIMEOUT,
        )
        r.raise_for_status()
        out = r.json()
        _log_moments_call("comment_moment", params, out)
        return out
    except Exception as e:
        _log_moments_call("comment_moment", params, f"Error: {e}")
        raise


def get_comments(comment_ids: list) -> dict:
    """
    根据评论 id 列表批量拉取评论。
    返回: { "message": "ok", "data": 评论列表 }
    """
    _check_requests()
    if not isinstance(comment_ids, list) or not comment_ids:
        return {"message": "缺少 commentIds 数组", "data": []}
    r = requests.post(
        _url("/comment/get"),
        json={"commentIds": comment_ids},
        headers=_headers(),
        timeout=MOMENTS_API_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def like_comment(comment_id: str, like: bool) -> dict:
    """
    点赞/取消点赞评论。uid/token 从环境变量读取。
    返回: { "message": "ok", "data": { commentId, likes, hasLiked } }
    """
    _check_requests()
    r = requests.post(
        _url("/comment/like"),
        json={"commentId": comment_id, "like": like},
        headers=_headers(),
        timeout=MOMENTS_API_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def get_liked_comments() -> dict:
    """
    获取当前用户点赞过的评论 id 列表。uid/token 从环境变量读取。
    返回: { "message": "ok", "data": [id1, id2, ...] }
    """
    _check_requests()
    r = requests.get(
        _url("/comments/liked"),
        headers=_headers(),
        timeout=MOMENTS_API_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def like_moment(moment_id: str, like: bool) -> dict:
    """
    点赞/取消点赞动态。uid/token 从环境变量读取。
    返回: { "message": "ok", "data": { momentId, likes, hasLiked } }
    """
    _check_requests()
    r = requests.post(
        _url("/like"),
        json={"momentId": moment_id, "like": like},
        headers=_headers(),
        timeout=MOMENTS_API_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def get_liked_moments() -> dict:
    """
    获取当前用户点赞过的动态 id 列表。uid/token 从环境变量读取。
    返回: { "message": "ok", "data": [id1, id2, ...] }
    """
    _check_requests()
    r = requests.get(
        _url("/liked"),
        headers=_headers(),
        timeout=MOMENTS_API_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()
