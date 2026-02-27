"""
动态（Moments）后端 API 客户端。
与 Express 提供的 /api/moments 接口字段与路径一致，供 Thinking / 工具模型调用。
uid 与 token 从环境变量 MOMENTS_UID、MOMENTS_TOKEN 读取，请求头必带。
调用动态相关工具时写入 moments_tools.log，记录工具名、参数及结果摘要。
"""
import os
import json
from pathlib import Path
from typing import Optional, Any, Dict, List
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

# 模块级缓冲区：存储最近一次 get_moments / get_comments 的结果，供后续工具参考
_MOMENTS_BUFFER: List[Dict[str, Any]] = []            # 最近一次 moments 列表（已规整）
_MOMENTS_BY_ID: Dict[str, Dict[str, Any]] = {}        # moment_id -> moment 简化信息
_MOMENT_COMMENTS_IDS: Dict[str, List[str]] = {}       # moment_id -> [comment_id,...]
_COMMENTS_BY_ID: Dict[str, Dict[str, Any]] = {}       # comment_id -> 简化后的 comment 信息


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


def _request_with_retry(
    tool_name: str,
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: Optional[int] = None,
    log_params: Optional[dict] = None,
    max_retries: int = 3,
) -> dict:
    """
    统一的 HTTP 请求封装：最多重试 max_retries 次，仍失败则记录失败日志并抛出异常交由上游处理。
    """
    _check_requests()
    url = _url(path)
    timeout = timeout or MOMENTS_API_TIMEOUT
    log_params = log_params or {}

    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            if method.upper() == "GET":
                resp = requests.get(
                    url,
                    params=params,
                    headers=_headers(),
                    timeout=timeout,
                )
            elif method.upper() == "POST":
                resp = requests.post(
                    url,
                    params=params,
                    json=json_body,
                    headers=_headers(),
                    timeout=timeout,
                )
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            resp.raise_for_status()
            out = resp.json()
            _log_moments_call(f"{tool_name}#ok{attempt}", log_params, out)
            return out
        except Exception as e:
            last_exc = e
            _log_moments_call(f"{tool_name}#error{attempt}", log_params, f"Error: {e}")
            if attempt < max_retries:
                continue
            # 最终失败仍抛出，让调用方（主模型/工具模型）感知错误
            raise

    # 理论上不会到这里
    if last_exc:
        raise last_exc
    raise RuntimeError("Unknown error in _request_with_retry")


def _normalize_moment_item(item: dict) -> dict:
    """
    将后端返回的单条 moment 文档规整成仅包含需要字段的结构：
    _id, title, content, filenames, comments, likes, views, createdAt, username。
    """
    moment_id = item.get("_id") or item.get("documentId")
    if not moment_id:
        return {}

    content = (item.get("content") or "").replace("\r\n", "\n")

    filenames = item.get("filenames") or {}
    comments = item.get("comments") or []

    return {
        "_id": moment_id,
        "title": item.get("title") or "",
        "content": content,
        "filenames": filenames,
        "comments": comments,
        "likes": item.get("likes", 0),
        "views": item.get("views", 0),
        "createdAt": item.get("createdAt"),
        "username": item.get("username"),
    }


def get_moments() -> dict:
    """
    获取「已发布动态列表」并写入本模块缓冲区。

    - 工具名（在工具系统中）：get_moments
    - 总是 **先调用本工具**，后续再调用 comment_moment / get_comments / like_moment / like_comment / analyze_moment_images 等
    - 返回结构:
      { "message": "...", "data": [ { "_id", "title", "content", "filenames", "comments", "likes", "views", "createdAt", "username" }, ... ] }
        - filenames: { filename: url, ... }
        - comments:  初始为评论 id 列表，调用 get_comments 后会被升级为 { comment_id: comment_info }
    - 会自动写入模块级缓冲区：_MOMENTS_BUFFER / _MOMENTS_BY_ID / _MOMENT_COMMENTS_IDS，供后续工具使用
    - 网络请求最多重试 3 次，失败会在 moments_tools.log 中记录错误并抛出异常
    """
    global _MOMENTS_BUFFER, _MOMENTS_BY_ID, _MOMENT_COMMENTS_IDS

    params = {"isEditing": "false"}
    raw = _request_with_retry(
        "get_moments",
        "GET",
        "/get",
        params=params,
        log_params=params,
    )

    data = raw.get("data") or []
    normalized: List[Dict[str, Any]] = []
    moments_by_id: Dict[str, Dict[str, Any]] = {}
    moment_comments_ids: Dict[str, List[str]] = {}

    for item in data:
        if not isinstance(item, dict):
            continue
        norm = _normalize_moment_item(item)
        moment_id = norm.get("_id")
        if not moment_id:
            continue
        normalized.append(norm)
        moments_by_id[moment_id] = norm
        # 记录 comments 的 id 列表，以便后续 get_comments / comment_moment 使用
        comment_ids = norm.get("comments") or []
        if isinstance(comment_ids, list):
            moment_comments_ids[moment_id] = list(comment_ids)

    _MOMENTS_BUFFER = normalized
    _MOMENTS_BY_ID = moments_by_id
    _MOMENT_COMMENTS_IDS = moment_comments_ids

    out = {"message": raw.get("message", ""), "data": normalized}
    # 再记录一条整体结构日志，便于调试（不包含原始大字段）
    _log_moments_call("get_moments_structured", params, out)
    return out


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
    面向大模型的简化版 get_moments（推荐使用）。

    - 工具名（在工具系统中）：get_moments
    - 内部直接调用 get_moments，因此缓冲区与 get_moments 完全一致
    - 返回结构与 get_moments 相同，只是日志中会标记为 simple，便于区分
    - 若只需要阅读/筛选动态内容，一般调用本函数即可
    """
    params = {"isEditing": "false", "mode": "simple"}
    raw = get_moments()
    # 直接复用 get_moments 已经规整并写入缓冲区的结构
    out = {"message": raw.get("message", ""), "data": raw.get("data") or []}
    _log_moments_call("get_moments_simple", params, out)
    return out


def add_moment(title: str, content: str = "") -> dict:
    """
    发布一条新的动态（仅含标题和正文）。

    - 工具名（在工具系统中）：add_moment
    - published 固定为 true，acknowledge 固定为 false，不包含图片/文件
    - 参数:
      - title: 必填，动态标题
      - content: 选填，正文内容，内部会将 \\r\\n 转为 \\n
    - 返回: { "message": "ok", "data": 后端原始 moment 文档 }
    - 成功后会将新动态规整后写入本模块缓冲区（最新一条在列表最前）
    - 网络请求最多重试 3 次，失败会在 moments_tools.log 中记录错误并抛出异常
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
    out = _request_with_retry(
        "add_moment",
        "POST",
        "/post",
        json_body=payload,
        log_params=params,
    )

    # 成功发布后尝试更新本地 moments 缓冲
    try:
        created = out.get("data") or {}
        if isinstance(created, dict):
            norm = _normalize_moment_item(created)
            moment_id = norm.get("_id")
            if moment_id:
                _MOMENTS_BY_ID[moment_id] = norm
                _MOMENT_COMMENTS_IDS.setdefault(moment_id, [])
                # 追加到缓冲列表头部（最新在前）
                _MOMENTS_BUFFER.insert(0, norm)
    except Exception:
        pass

    return out


def comment_moment(moment_id: str, comment: str, belong: Optional[str] = None) -> dict:
    """
    对某条动态或某条评论发表评论。

    - 工具名（在工具系统中）：comment_moment
    - 用法建议:
      1) 先调用 get_moments，找到目标 moment 并拿到其 "_id"（moment_id）
      2) 若直接回复动态本身：belong 传 None
      3) 若回复某条评论：
         - 从 moment["comments"] 里拿到评论 id 列表
         - 调用 get_comments(comment_ids) 获取每条评论的详细内容
         - 选择需要回复的评论，其 "_id" 作为 belong 传入
    - 参数:
      - moment_id: 源动态的 "_id"（必须来自最近一次 get_moments）
      - comment: 回复内容文本
      - belong: 被回复的评论 id，可为 None
    - 返回: { "message": "ok", "data": 新评论文档 }
    - 会自动更新本模块评论缓冲和对应 moment 的 comments 字段
    - 网络请求最多重试 3 次，失败会在 moments_tools.log 中记录错误并抛出异常
    """
    _check_requests()
    body = {"momentId": moment_id, "comment": comment}
    if belong is not None and str(belong).strip():
        body["belong"] = str(belong).strip()
    params = {"moment_id": moment_id, "comment": comment, "belong": belong}
    out = _request_with_retry(
        "comment_moment",
        "POST",
        "/comment/post",
        json_body=body,
        log_params=params,
    )

    # 更新本地评论缓冲与对应 moment 的 comments 列表
    try:
        created = out.get("data") or {}
        if isinstance(created, dict):
            cid = created.get("_id")
            if cid:
                simple = {
                    "_id": cid,
                    "content": created.get("content") or "",
                    "username": created.get("username"),
                    "momentId": created.get("momentId") or moment_id,
                    "likes": created.get("likes", 0),
                    "createdAt": created.get("createdAt"),
                }
                _COMMENTS_BY_ID[cid] = simple
                mid = simple["momentId"]
                _MOMENT_COMMENTS_IDS.setdefault(mid, [])
                if cid not in _MOMENT_COMMENTS_IDS[mid]:
                    _MOMENT_COMMENTS_IDS[mid].append(cid)
                # 如果 moments 缓冲中存在该 moment，确保其 comments 字段包含该 comment id
                m = _MOMENTS_BY_ID.get(mid)
                if m is not None:
                    comments_field = m.get("comments")
                    if isinstance(comments_field, list):
                        if cid not in comments_field:
                            comments_field.append(cid)
                    elif isinstance(comments_field, dict):
                        comments_field[cid] = simple
    except Exception:
        pass

    return out


def get_comments(comment_ids: list) -> dict:
    """
    根据评论 id 列表批量拉取评论详情。

    - 工具名（在工具系统中）：get_comments
    - 常见用法：
      1) 先调用 get_moments，选中某条动态 A
      2) 从 A["comments"] 读出评论 id 列表（若是映射则取 keys）
      3) 把这个 id 列表传给本工具，获取每条评论的详细内容
    - 返回: { "message": "ok", "data": [ { "_id", "content", "username", "momentId", "likes", "createdAt" }, ... ] }
    - 会更新模块级评论缓冲 (_COMMENTS_BY_ID / _MOMENT_COMMENTS_IDS)，并将对应 moment 的 comments 字段升级为 {comment_id: comment_info} 映射
    - 网络请求最多重试 3 次，失败会在 moments_tools.log 中记录错误并抛出异常
    """
    if not isinstance(comment_ids, list) or not comment_ids:
        return {"message": "缺少 commentIds 数组", "data": []}

    raw = _request_with_retry(
        "get_comments",
        "POST",
        "/comment/get",
        json_body={"commentIds": comment_ids},
        log_params={"commentIds": comment_ids},
    )

    data = raw.get("data") or []
    simplified: List[Dict[str, Any]] = []

    for c in data:
        if not isinstance(c, dict):
            continue
        cid = c.get("_id")
        if not cid:
            continue
        mid = c.get("momentId")
        simple = {
            "_id": cid,
            "content": c.get("content") or "",
            "username": c.get("username"),
            "momentId": mid,
            "likes": c.get("likes", 0),
            "createdAt": c.get("createdAt"),
        }
        simplified.append(simple)
        _COMMENTS_BY_ID[cid] = simple
        if mid:
            _MOMENT_COMMENTS_IDS.setdefault(mid, [])
            if cid not in _MOMENT_COMMENTS_IDS[mid]:
                _MOMENT_COMMENTS_IDS[mid].append(cid)

    # 将 moments 缓冲中的 comments 字段从 id 列表升级为 {id: comment_info} 映射
    try:
        comments_by_moment: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for c in simplified:
            mid = c.get("momentId")
            cid = c.get("_id")
            if not mid or not cid:
                continue
            comments_by_moment.setdefault(mid, {})[cid] = c

        for mid, mapping in comments_by_moment.items():
            m = _MOMENTS_BY_ID.get(mid)
            if m is None:
                continue
            m["comments"] = mapping
    except Exception:
        pass

    out = {"message": raw.get("message", ""), "data": simplified}
    _log_moments_call("get_comments_structured", {"commentIds": comment_ids}, out)
    return out


def like_comment(comment_id: str, like: bool) -> dict:
    """
    点赞/取消点赞评论。uid/token 从环境变量读取。

    - 工具名（在工具系统中）：like_comment
    - 参数:
      - comment_id: 目标评论的 "_id"，通常从 get_comments 结果或缓冲中获取
      - like: True 表示点赞，False 表示取消点赞
    - 返回: { "message": "ok", "data": { "commentId", "likes", "hasLiked" } }
    - 成功后会更新本模块缓冲中该评论的 likes 计数
    - 网络请求最多重试 3 次，失败会在 moments_tools.log 中记录错误并抛出异常
    """
    out = _request_with_retry(
        "like_comment",
        "POST",
        "/comment/like",
        json_body={"commentId": comment_id, "like": like},
        log_params={"commentId": comment_id, "like": like},
    )

    # 更新本地评论 likes 计数
    try:
        data = out.get("data") or {}
        cid = data.get("commentId") or comment_id
        likes_cnt = data.get("likes")
        if cid in _COMMENTS_BY_ID and likes_cnt is not None:
            _COMMENTS_BY_ID[cid]["likes"] = likes_cnt
    except Exception:
        pass

    return out


def get_liked_comments() -> dict:
    """
    获取当前用户点赞过的评论 id 列表。uid/token 从环境变量读取。

    - 工具名（在工具系统中）：get_liked_comments
    - 返回: { "message": "ok", "data": [comment_id1, comment_id2, ...] }
    - 可与 _COMMENTS_BY_ID / get_comments 结合，恢复这些评论的详细内容
    - 网络请求最多重试 3 次，失败会在 moments_tools.log 中记录错误并抛出异常
    """
    return _request_with_retry(
        "get_liked_comments",
        "GET",
        "/comments/liked",
        log_params={},
    )


def like_moment(moment_id: str, like: bool) -> dict:
    """
    点赞/取消点赞动态。uid/token 从环境变量读取。

    - 工具名（在工具系统中）：like_moment
    - 参数:
      - moment_id: 目标动态的 "_id"，通常从 get_moments 结果或缓冲中获取
      - like: True 表示点赞，False 表示取消点赞
    - 返回: { "message": "ok", "data": { "momentId", "likes", "hasLiked" } }
    - 成功后会更新本模块缓冲中该动态的 likes 计数
    - 网络请求最多重试 3 次，失败会在 moments_tools.log 中记录错误并抛出异常
    """
    out = _request_with_retry(
        "like_moment",
        "POST",
        "/like",
        json_body={"momentId": moment_id, "like": like},
        log_params={"momentId": moment_id, "like": like},
    )

    # 更新本地 moments 缓冲中的 likes 计数
    try:
        data = out.get("data") or {}
        mid = data.get("momentId") or moment_id
        likes_cnt = data.get("likes")
        if mid in _MOMENTS_BY_ID and likes_cnt is not None:
            _MOMENTS_BY_ID[mid]["likes"] = likes_cnt
    except Exception:
        pass

    return out


def get_liked_moments() -> dict:
    """
    获取当前用户点赞过的动态 id 列表。uid/token 从环境变量读取。

    - 工具名（在工具系统中）：get_liked_moments
    - 返回: { "message": "ok", "data": [moment_id1, moment_id2, ...] }
    - 可与 _MOMENTS_BY_ID / get_moments 结合，恢复这些动态的详细内容
    - 网络请求最多重试 3 次，失败会在 moments_tools.log 中记录错误并抛出异常
    """
    return _request_with_retry(
        "get_liked_moments",
        "GET",
        "/liked",
        log_params={},
    )


def analyze_moment_images(moment_id: str, max_images: int = 3, timeout: int = 25) -> dict:
    """
    对某条动态中的图片进行识别，返回图片文字描述（不返回图片本身）。

    - 工具名（在工具系统中）：analyze_moment_images
    - 用法建议:
      1) 先调用 get_moments，选中含图片的动态 A，并拿到其 "_id" 作为 moment_id
      2) 调用本工具：analyze_moment_images(moment_id=..., max_images=3)
      3) 将返回的 data（多张图片描述按行拼接）作为参考，再结合该动态的文字内容进行回复
    - 行为说明:
      - 从缓冲区 _MOMENTS_BY_ID 中读取 moment.filenames（value 为 URL）
      - 最多分析 max_images 张图片
      - 使用与 QQ 图片分析相同的 DashScope 视觉模型进行识别
    - 返回:
      { "message": "ok" | 其他错误信息, "data": 图片描述字符串（可能为空） }
    - 下载图片和调用视觉模型时内部也会进行最多 3 次重试，并将错误写入 moments_tools.log
    """
    moment = _MOMENTS_BY_ID.get(moment_id)
    if not moment:
        return {"message": f"moment_id '{moment_id}' not found in buffer", "data": ""}

    filenames = moment.get("filenames") or {}
    if not filenames:
        return {"message": "no_images", "data": ""}

    image_urls = list(filenames.values())[:max_images]

    # 使用与 QQ 图片相同的 DashScope VLM 进行识别
    try:
        import requests as _requests
        import base64 as _b64
        from config import (
            DASHSCOPE_API_URL,
            DASHSCOPE_API_KEY,
            DASHSCOPE_VISION_MODEL,
            DASHSCOPE_API_TIMEOUT,
            require_env,
        )
    except Exception as e:
        msg = f"import_error: {e}"
        _log_moments_call("analyze_moment_images", {"moment_id": moment_id}, msg)
        return {"message": msg, "data": ""}

    api_key = require_env("DASHSCOPE_API_KEY", DASHSCOPE_API_KEY)
    if not api_key:
        msg = "no_dascope_api_key"
        _log_moments_call("analyze_moment_images", {"moment_id": moment_id}, msg)
        return {"message": msg, "data": ""}

    descriptions: List[str] = []

    for idx, url in enumerate(image_urls, start=1):
        image_content = url

        if not isinstance(url, str) or not url.startswith("http"):
            # 非 http URL 暂时跳过
            continue

        # 下载图片并尝试转成 data URL（如失败则直接用原 URL）
        try:
            dl_attempts = 3
            img_resp = None
            for attempt in range(1, dl_attempts + 1):
                try:
                    img_resp = _requests.get(
                        url,
                        timeout=10,
                        headers={
                            "User-Agent": "Mozilla/5.0",
                        },
                    )
                    break
                except Exception as e:
                    if attempt == dl_attempts:
                        _log_moments_call("analyze_moment_images_download", {"url": url}, f"Error: {e}")
            if img_resp is not None and img_resp.status_code == 200 and len(img_resp.content) > 100:
                img_b64 = _b64.b64encode(img_resp.content).decode("utf-8")
                content_type = img_resp.headers.get("Content-Type", "image/jpeg")
                if "png" in content_type:
                    image_content = f"data:image/png;base64,{img_b64}"
                elif "gif" in content_type:
                    image_content = f"data:image/gif;base64,{img_b64}"
                else:
                    image_content = f"data:image/jpeg;base64,{img_b64}"
        except Exception as dl_err:
            _log_moments_call("analyze_moment_images_download", {"url": url}, f"Error: {dl_err}")
            image_content = url

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": DASHSCOPE_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "请详细描述这张图片的内容。如果包含文字请提取出来。如果是表情包/梗图请描述其含义。简洁回复，不超过100字。",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_content,
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 200,
        }

        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = _requests.post(
                    DASHSCOPE_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=timeout or DASHSCOPE_API_TIMEOUT,
                    proxies={"http": None, "https": None},
                )
                if resp.status_code == 200:
                    result = resp.json()
                    desc = (
                        result.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    if desc:
                        descriptions.append(f"[图片{idx}内容: {desc.strip()}]")
                    else:
                        descriptions.append(f"[图片{idx}: 无法识别内容]")
                    break
                else:
                    last_err = RuntimeError(f"VLM status {resp.status_code}")
            except Exception as e:
                last_err = e

        if last_err is not None and (len(descriptions) < idx):
            _log_moments_call(
                "analyze_moment_images_vlm",
                {"url": url, "attempts": 3},
                f"Error: {last_err}",
            )
            descriptions.append(f"[图片{idx}: 分析出错]")

    joined = "\n".join(descriptions)
    return {"message": "ok", "data": joined}
