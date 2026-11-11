"""Resolve sticker candidates from authorized QQ sources without saving or sending."""
from __future__ import annotations

import copy
import json
import re

from chatBot.messages import segments

_KINDS = frozenset({"image", "face", "mface"})


def _message_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value or "")):
        raise ValueError("message_id 必须是有效的 QQ 消息编号")
    return str(value)


def _raw(value):
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        data = None
    return data if isinstance(data, dict) else {}


def _parts(message):
    return segments(message.get("message", message.get("raw_message", "")))


def _candidates(message):
    return [part for part in _parts(message) if part.get("type") in _KINDS]


def _authorized_event(service, identity, event_id):
    if identity.local or not identity.account or not identity.user or not event_id:
        raise PermissionError("消息表情来源需要当前授权的 QQ 会话事件")
    with service.store.connection() as db:
        row = db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()
    event = _raw(row["raw"]) if row else {}
    if (not event or event.get("post_type") not in {"message", "message_sent", "bines_proactive"}
            or service.store.identity(event) != identity):
        raise PermissionError("表情来源事件不属于当前 QQ 身份")
    return event


def _reply_ids(event):
    # Scheduler data cannot grant an uncached message through a reply segment.
    if event.get("post_type") == "bines_proactive":
        return []
    replies = []
    for part in _parts(event):
        if part.get("type") == "reply":
            data = part.get("data")
            if not isinstance(data, dict):
                raise ValueError("引用消息段缺少有效 data")
            referenced = _message_id(data.get("id"))
            if referenced not in replies:
                replies.append(referenced)
    return replies


def _source_row(service, identity, message_id, *, required):
    # Do not use qq_admin/owner authorization here: account administration does
    # not turn a message number from another conversation into a source grant.
    external = f"{identity.account}:{identity.group or 'p:'+identity.user}:{message_id}"
    with service.store.connection() as db:
        row = db.execute("SELECT raw,recalled FROM messages WHERE scope=? AND external_id=?",
                         (identity.scope, external)).fetchone()
    if row and row["recalled"]:
        raise PermissionError("表情来源消息已撤回")
    if required and not row:
        raise PermissionError("表情来源消息不存在或不属于当前 QQ 会话")
    return row


def _check_peer(data, identity, message_id, *, cached):
    if str(data.get("message_id")) != message_id:
        raise PermissionError("刷新结果的消息编号与表情来源不一致")
    if str(data.get("self_id", identity.account)) != identity.account:
        raise PermissionError("刷新结果不属于当前 QQ 账号")
    if identity.group:
        valid = (data.get("message_type") == "group"
                 and str(data.get("group_id")) == identity.group)
    else:
        sender = str(data.get("user_id") or (data.get("sender") or {}).get("user_id") or "")
        peer = str(data.get("target_id") or data.get("peer_id") or "")
        # Some get_msg responses omit the target of our own sent message. A
        # previously authorized local row can establish that missing peer, but
        # must never override an explicitly different target in the response.
        outgoing = sender == identity.account and (peer == identity.user or (not peer and cached))
        valid = (data.get("message_type") == "private" and not data.get("group_id")
                 and (sender == identity.user or outgoing)
                 and (not peer or peer in {identity.user, identity.account}))
    if not valid:
        raise PermissionError("刷新结果不属于当前 QQ 会话")


def _stored_source(service, identity, message_id):
    row = _source_row(service, identity, message_id, required=True)
    data = _raw(row["raw"])
    refreshed = False
    # Face IDs and marketplace metadata are not expiring image URLs. Image
    # sources, including the current incoming message, are refreshed on demand.
    if not data or any(part.get("type") == "image" for part in _parts(data)):
        try:
            result = service.client.raw_api("get_msg", {"message_id": message_id}, timeout=10)
        except (OSError, RuntimeError, ValueError):
            result = None
        if isinstance(result, dict) and result.get("status") == "ok":
            fresh = result.get("data")
            if not isinstance(fresh, dict):
                raise PermissionError("刷新表情来源的返回格式无效")
            _check_peer(fresh, identity, message_id, cached=True)
            data, refreshed = fresh, True
    _source_row(service, identity, message_id, required=True)
    return data, refreshed


def _quoted_source(service, identity, event_id, message_id):
    cached = _source_row(service, identity, message_id, required=False)
    result = service.quoted_message(identity, event_id, message_id)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValueError("引用的表情来源暂不可读取或已撤回")
    data = result.get("data")
    if not isinstance(data, dict):
        raise PermissionError("引用表情来源的返回格式无效")
    _check_peer(data, identity, message_id, cached=cached is not None)
    _source_row(service, identity, message_id, required=False)
    # quoted_message may intentionally return its authorized cache when the
    # refresh fails. Do not claim that this URL has definitely been refreshed.
    return data, None


def _selected(part):
    data = part.get("data")
    if not isinstance(data, dict):
        raise ValueError("所选表情消息段缺少有效 data")
    kind = part["type"]
    if kind == "image" and not any(isinstance(data.get(key), str) and data[key]
                                   for key in ("url", "file")):
        raise ValueError("所选图片没有可读取的 url 或 file")
    if kind == "face" and (data.get("id") is None or str(data["id"]) == ""):
        raise ValueError("所选 QQ 表情缺少 id")
    if kind == "mface" and not all(data.get(key) for key in ("emoji_package_id", "emoji_id", "key")):
        raise ValueError("所选商城表情缺少 emoji_package_id、emoji_id 或 key")
    return copy.deepcopy({"type": kind, "data": data})


def validate_sticker_source(service, identity, event_id, source_message_id):
    """Recheck a previously resolved source after download, without any API call.

    The gateway separately checks the live proactive lease/policy. An uncached
    real reply remains authorized, but any local withdrawal tombstone wins.
    """
    event = _authorized_event(service, identity, event_id)
    source_id = _message_id(source_message_id)
    if event.get("post_type") != "bines_proactive":
        current_id = _message_id(event.get("message_id"))
        _source_row(service, identity, current_id, required=True)
    _source_row(service, identity, source_id, required=source_id not in _reply_ids(event))
    return True


def resolve_sticker_source(service, identity, event_id, message_id=None, image_index=0):
    """Return one image/face/mface from this event, a reply, or a scoped message.

    ``image_index`` is zero based over those three segment types only. Current
    message candidates take precedence; replies are used only when it has none.
    The returned segment and IDs are internal source data, not a public receipt.
    Proactive callers must first pass the gateway's live proactive guard and
    provide an explicit message_id; a scheduler event is never an image source.
    No downloads, model calls, database writes or QQ writes occur here.
    """
    if type(image_index) is not int or image_index < 0:
        raise ValueError("image_index 必须是从 0 开始的非负整数")
    event = _authorized_event(service, identity, event_id)
    proactive = event.get("post_type") == "bines_proactive"
    if proactive and message_id is None:
        raise ValueError("自主收藏历史表情必须明确指定当前 QQ 会话的 message_id")
    current_id = None if proactive else _message_id(event.get("message_id"))
    if current_id:
        _source_row(service, identity, current_id, required=True)
    replies = _reply_ids(event)
    sources = []
    if message_id is not None:
        requested = _message_id(message_id)
        if requested in replies and requested != current_id:
            data, refreshed = _quoted_source(service, identity, event_id, requested)
        else:
            data, refreshed = _stored_source(service, identity, requested)
        sources.append((requested, data, refreshed))
    elif _candidates(event):
        data, refreshed = _stored_source(service, identity, current_id)
        sources.append((current_id, data, refreshed))
    elif replies:
        if len(replies) > 4:
            raise ValueError("本轮引用超过 4 条，请用 message_id 指定表情来源")
        for referenced in replies:
            data, refreshed = _quoted_source(service, identity, event_id, referenced)
            sources.append((referenced, data, refreshed))
    else:
        raise ValueError("当前消息没有图片、QQ 表情或商城表情，也没有可用的引用消息")
    candidates = [(mid, part, refreshed) for mid, data, refreshed in sources for part in _candidates(data)]
    if not candidates:
        raise ValueError("指定来源中没有图片、QQ 表情或商城表情")
    if image_index >= len(candidates):
        raise ValueError(f"image_index 超出范围；来源只有 {len(candidates)} 个图片或表情候选")
    selected_id, part, refreshed = candidates[image_index]
    # Close the network refresh interval with another withdrawal check.
    validate_sticker_source(service, identity, event_id, selected_id)
    return {"segment": _selected(part), "source_message_id": selected_id,
            "source_refreshed": refreshed}


def resolve_favorite_source(service, identity, favorite_md5, count=100):
    """Select an existing account favorite by MD5, never by a shifting index."""
    if not identity.privileged:
        raise PermissionError("读取账号收藏表情仅允许本机或主人私聊")
    if not isinstance(favorite_md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", favorite_md5):
        raise ValueError("favorite_md5 必须是收藏表情的 32 位 MD5")
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("count 必须是 1 到 1000 之间的整数")
    result = service.client.raw_api("fetch_custom_face_detail", {"count": count}, timeout=30)
    if not isinstance(result, dict) or result.get("status") != "ok" or not isinstance(result.get("data"), list):
        raise ValueError("QQ 收藏表情暂不可读取")
    wanted = favorite_md5.lower()
    row = next((item for item in result["data"] if isinstance(item, dict)
                and str(item.get("md5") or "").lower() == wanted), None)
    if row is None:
        raise ValueError("返回的 QQ 收藏列表中没有该 MD5，请刷新收藏列表后重试")
    url = row.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError("该 QQ 收藏表情没有可读取的图片 URL")
    return {
        "segment": {"type": "image", "data": {"file": url, "sub_type": 1,
                   "summary": row.get("desc") or row.get("ocrWord") or "[收藏表情]"}},
        "source_message_id": None,
        "favorite": {"md5": wanted, "res_id": row.get("resId") or row.get("res_id"),
                     "emoji_id": row.get("emojiId", row.get("emoji_id")),
                     "package_id": row.get("packageId", row.get("package_id"))},
    }
