"""Lossless OneBot envelopes and stable, account-specific conversation identities."""
from dataclasses import dataclass
import html
import json
import re


def cq_unescape(value):
    return html.unescape(str(value).replace("&#44;", ","))


def segments(message):
    if isinstance(message, dict):
        return [message]
    if isinstance(message, list):
        return [part for part in message if isinstance(part, dict)]
    text = str(message or "")
    result, cursor = [], 0
    for match in re.finditer(r"\[CQ:([a-zA-Z0-9_-]+)((?:,[^\]]*)?)\]", text):
        if match.start() > cursor:
            result.append({"type": "text", "data": {"text": cq_unescape(text[cursor:match.start()])}})
        data = {}
        for item in match.group(2).lstrip(",").split(","):
            key, sep, value = item.partition("=")
            if sep:
                data[key] = cq_unescape(value)
        result.append({"type": match.group(1), "data": data})
        cursor = match.end()
    if cursor < len(text):
        result.append({"type": "text", "data": {"text": cq_unescape(text[cursor:])}})
    return result


SEND_ACTIONS = frozenset({
    "send_msg", "send_private_msg", "send_group_msg",
    "send_forward_msg", "send_private_forward_msg", "send_group_forward_msg",
})
FORWARD_ACTIONS = frozenset(action for action in SEND_ACTIONS if "forward" in action)


def outgoing_segments(message, auto_escape=False):
    """Validate the same single-segment/list/CQ forms that NapCat normalizes."""
    if isinstance(message, str):
        chain = [{"type": "text", "data": {"text": message}}] if auto_escape else segments(message)
    elif isinstance(message, dict):
        chain = [message]
    elif isinstance(message, list):
        chain = message
    else:
        raise ValueError("message 必须是文本、消息段对象或消息段数组")
    if not chain or any(
        not isinstance(part, dict) or not isinstance(part.get("type"), str)
        or not isinstance(part.get("data"), dict) for part in chain
    ):
        raise ValueError("每个消息段必须包含 type 和 data 对象")
    if any(part["type"] == "node" for part in chain) and any(part["type"] != "node" for part in chain):
        raise ValueError("合并转发 node 不能与普通消息段混在同一次发送中")
    return chain


def sent_message(params):
    """Forward actions accept messages; ordinary sends use message."""
    return params.get("messages", params.get("message"))


def readable(message):
    parts = []
    for segment in segments(message):
        kind, data = segment.get("type"), segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            parts.append(f"[@QQ:{data.get('qq')}]")
        else:
            parts.append(f"[{kind}:{json.dumps(data, ensure_ascii=False)}]")
    return "".join(parts)


@dataclass(frozen=True)
class Identity:
    account: str
    user: str
    group: str = ""
    name: str = ""
    group_role: str = "member"
    owner: bool = False
    local: bool = False

    @property
    def privileged(self):
        # A group is public even when its current speaker owns the local assistant.
        return self.local or (self.owner and not self.group)

    @property
    def scope(self):
        if self.local:
            return "owner"
        if self.group:
            return f"qq:{self.account}:group:{self.group}"
        return "owner" if self.owner else f"qq:{self.account}:private:{self.user}"

    @property
    def conversation(self):
        if self.local:
            return "local"
        return f"qq:{self.account}:{'g:'+self.group if self.group else 'p'}:u:{self.user}"

    @property
    def state_scope(self):
        return f"{self.scope}:user:{self.user}" if self.group else self.scope

    @classmethod
    def from_event(cls, event, owners=()):
        sender = event.get("sender") or {}
        user = str(event.get("user_id") or sender.get("user_id") or "")
        account = str(event.get("self_id") or "")
        if user == account and not event.get("group_id"):
            user = str(event.get("target_id") or event.get("peer_id") or user)
        return cls(
            account=account,
            user=user,
            group=str(event.get("group_id") or "") if event.get("message_type") != "private" else "",
            name=str(sender.get("card") or sender.get("nickname") or user),
            group_role=str(sender.get("role") or "member"),
            owner=user in owners,
        )


LOCAL_IDENTITY = Identity("local", "owner", local=True, owner=True)
