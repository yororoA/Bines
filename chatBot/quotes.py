"""Unambiguous reply IDs and trusted authorship for QQ quote envelopes."""
import json
import re

from chatBot.messages import segments


def message_id(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not re.fullmatch(r"[1-9][0-9]*", str(value))):
        raise ValueError("引用需使用真实的正整数 message_id")
    return str(value)


def validate_reply_segments(message, *, auto_escape=False):
    """Native and convenience sends must agree on a single reply per message."""
    if isinstance(message, str) and auto_escape:
        return
    replies = 0
    for part in segments(message):
        data = part.get("data") or {}
        if part.get("type") == "reply":
            if not isinstance(data, dict):
                raise ValueError("引用消息段需要 data 对象")
            message_id(data.get("id"))
            replies += 1
            if replies > 1:
                raise ValueError("一条消息只能引用一条原消息；reply_to 不能与 reply 消息段并用")
        elif part.get("type") == "node" and isinstance(data, dict) and "content" in data:
            validate_reply_segments(data["content"])


def cached_quote(identity, quoted_id, row):
    """Receipt-only rows lack OneBot sender fields; row.actor is the peer, not us."""
    if not row or not row["raw"]:
        return None
    try:
        data = json.loads(row["raw"])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not any(key in data for key in ("message", "raw_message")):
        return None
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    assistant = row["role"] == "assistant"
    author = identity.account if assistant else str(data.get("user_id") or sender.get("user_id") or row["actor"])
    # An assistant receipt is stored under the destination identity. Its name
    # and actor may therefore be the other speaker's; do not use either as ours.
    if assistant and str(sender.get("user_id") or data.get("user_id") or "") != author:
        sender = {}
    sender = {**sender, "user_id": author}
    if not (sender.get("card") or sender.get("nickname")):
        sender["nickname"] = "Bines" if assistant else (row["name"] or author)
    result = {**data, "message_id": quoted_id, "self_id": identity.account,
              "message_type": "group" if identity.group else "private",
              "user_id": author, "sender": sender}
    if identity.group:
        result["group_id"] = identity.group
    else:
        result["target_id"] = identity.user if assistant else identity.account
    return result


def validate_quote_peer(data, identity, quoted_id, *, cached=False):
    if not isinstance(data, dict):
        raise PermissionError("引用消息返回格式无效")
    same_message = str(data.get("message_id")) == quoted_id
    same_account = str(data.get("self_id", identity.account)) == identity.account
    if identity.group:
        same_peer = (data.get("message_type") == "group"
                     and str(data.get("group_id")) == identity.group)
    else:
        sender_data = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        sender = str(data.get("user_id") or sender_data.get("user_id") or "")
        peer = str(data.get("target_id") or data.get("peer_id") or "")
        # Cached provenance may fill an omitted outgoing peer, never override
        # an explicitly different peer in a refreshed get_msg response.
        outgoing = sender == identity.account and (peer == identity.user or (not peer and cached))
        same_peer = (data.get("message_type") == "private" and not data.get("group_id")
                     and (sender == identity.user or outgoing)
                     and (not peer or peer in {identity.user, identity.account}))
    if not (same_message and same_account and same_peer):
        raise PermissionError("引用消息不属于当前会话")
