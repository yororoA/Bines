"""Ground autonomous QQ destinations in trusted events or actual API results."""
from __future__ import annotations

import json
import threading

from chatBot.messages import SEND_ACTIONS


def _native_action(action):
    """NapCat registers these two suffix aliases for every native action."""
    if not isinstance(action, str):
        return ""
    for suffix in ("_async", "_rate_limited"):
        if action.endswith(suffix):
            action = action[:-len(suffix)]
            break
    return action


def send_action(action):
    action = _native_action(action)
    return action if action in SEND_ACTIONS else ""


def qq_id(value):
    """QQ identifiers are decimal strings, never booleans, labels or message text."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value)
    return value if (value.isascii() and value.isdecimal() and value[:1] != "0"
                     and len(value) <= 20) else ""


def destinations(action, params):
    """Mirror NapCat's actual private/group dispatch, including forward overrides."""
    action = send_action(action)
    if not action:
        return []
    if action == "send_private_msg":
        kind = "private"  # Its check() overwrites message_type.
    elif action == "send_group_msg":
        kind = "group"  # Its check() removes user_id and overwrites message_type.
    else:
        declared = params.get("message_type")
        if declared not in {None, "private", "group"}:
            raise ValueError("message_type 必须为 private 或 group")
        kind = declared or ("private" if action == "send_private_forward_msg" else
                            "group" if action == "send_group_forward_msg" or params.get("group_id") else "private")
    field = "group_id" if kind == "group" else "user_id"
    target = qq_id(params.get(field))
    if not target:
        raise ValueError(f"自主发送需要真实的 {field}；回复当前会话请用 napcat_send(message=...) 并省略收件人")
    result = [(kind, target)]
    # Private sends with group_id can fall back to sending the group if UID
    # lookup fails, so both possible destinations need independent provenance.
    if kind == "private" and params.get("group_id"):
        group = qq_id(params["group_id"])
        if not group:
            raise ValueError("临时私聊的 group_id 必须是真实群号")
        result.append(("group", group))
    return result


class RecipientRegistry:
    """Process-local list proofs; input messages stay in the durable gateway store."""
    def __init__(self, store):
        self.store = store
        self._known = {}
        self._lock = threading.Lock()

    def observe(self, identity, action, params, result):
        # A local call has no trusted QQ account envelope. Do not attribute it
        # to an account guessed from a model-supplied request or stale config.
        account = qq_id(identity.account) if not identity.local else ""
        action = _native_action(action)
        if (not account or action not in {"get_friend_list", "get_group_list", "get_group_member_list"}
                or not isinstance(result, dict) or result.get("status") != "ok"
                or type(result.get("retcode", 0)) is not int or result.get("retcode", 0) != 0
                or not isinstance(result.get("data"), list)):
            return
        found = set()
        for row in result["data"]:
            if not isinstance(row, dict):
                continue
            if action in {"get_friend_list", "get_group_member_list"}:
                user = qq_id(row.get("user_id"))
                if user:
                    found.add(("private", user))
            if action in {"get_group_list", "get_group_member_list"}:
                group = qq_id(row.get("group_id"))
                if group:
                    found.add(("group", group))
        with self._lock:
            self._known.setdefault(account, set()).update(found)

    def _inbound(self, account, kind, target):
        field = "json_extract(raw,'$.group_id')" if kind == "group" else (
            "COALESCE(json_extract(raw,'$.user_id'),json_extract(raw,'$.sender.user_id'))")
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT raw FROM events WHERE json_valid(raw) "
                "AND json_extract(raw,'$.post_type')='message' "
                "AND CAST(json_extract(raw,'$.self_id') AS TEXT)=? "
                f"AND CAST({field} AS TEXT)=? ORDER BY rowid DESC", (account, target))
            for row in rows:
                event = json.loads(row["raw"])
                sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
                actor = qq_id(event.get("user_id") or sender.get("user_id"))
                group = qq_id(event.get("group_id"))
                raw_mid = event.get("message_id")
                mid = str(raw_mid) if type(raw_mid) in {str, int} else ""
                if (not actor or actor == account or not mid
                        or event.get("message_type") != ("group" if group else "private")):
                    continue
                external = f"{account}:{group or 'p:'+actor}:{mid}"
                source = db.execute("SELECT role,recalled FROM messages WHERE external_id=? AND actor=?",
                                    (external, actor)).fetchone()
                if source and source["role"] == "user" and not source["recalled"]:
                    return True
        return False

    def authorize(self, identity, action, params, *, autonomous=False):
        if not autonomous or not send_action(action):
            return
        if action != send_action(action):
            raise PermissionError(
                f"自主发送请使用同步动作 {send_action(action)}；异步/限流别名的受理回执不等于已送达，消息尚未发送。")
        account = qq_id(identity.account) if not identity.local else ""
        if not account:
            raise PermissionError("自主 QQ 发送缺少可信账号身份")
        current = ("group", identity.group) if identity.group else ("private", identity.user)
        for target in destinations(action, params):
            if target == current:
                continue
            with self._lock:
                known = target in self._known.get(account, set())
            if not known and not self._inbound(account, *target):
                raise PermissionError(
                    "自主收件人没有可信来源，消息尚未发送。回复当前会话请用 napcat_send(message=...) 并省略ID；"
                    "跨会话需从真实入站记录或成功查询的好友/群/群成员列表选取，不能猜QQ号或群号。")
