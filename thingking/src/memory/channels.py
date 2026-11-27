"""Build ``ConversationEnvelope`` objects from Thinking's runtime inputs (local input, NapCat events)."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .models import (
    CHANNEL_LOCAL,
    CHANNEL_QQ_GROUP,
    CHANNEL_QQ_PRIVATE,
    LOCAL_CONVERSATION,
    OWNER_ACTOR,
    ConversationEnvelope,
    actor_for_qq,
    group_scope,
    private_scope,
)


def _qq_context(extra_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not extra_data:
        return {}
    return extra_data.get("qq_context") or extra_data


def is_qq_source(source: Optional[str]) -> bool:
    return str(source or "").strip().lower() == "qq"


def local_envelope(content: str = "", owner_name: str = "", timestamp: Optional[float] = None) -> ConversationEnvelope:
    return ConversationEnvelope(
        source="local",
        channel_type=CHANNEL_LOCAL,
        conversation_id=LOCAL_CONVERSATION,
        actor_id=OWNER_ACTOR,
        actor_name=owner_name,
        timestamp=time.time() if timestamp is None else timestamp,
        content=content,
    )


def qq_envelope(qq_context: Dict[str, Any], content: str = "", timestamp: Optional[float] = None) -> ConversationEnvelope:
    user_id = str(qq_context.get("user_id") or "").strip()
    group_id = str(qq_context.get("group_id") or "").strip()
    account = str(qq_context.get("self_id") or qq_context.get("account") or "").strip()
    if group_id in ("", "0", "None"):
        group_id = ""
    is_group = bool(group_id)
    return ConversationEnvelope(
        source="qq",
        channel_type=CHANNEL_QQ_GROUP if is_group else CHANNEL_QQ_PRIVATE,
        conversation_id=group_scope(group_id, account) if is_group else private_scope(user_id, account),
        actor_id=actor_for_qq(user_id),
        actor_name=str(qq_context.get("sender_name") or qq_context.get("sender") or ""),
        external_user_id=user_id,
        external_group_id=group_id,
        external_account_id=account,
        is_admin=bool(qq_context.get("is_admin")),
        timestamp=time.time() if timestamp is None else float(timestamp),
        content=content,
    )


def envelope_for_turn(
    source: Optional[str],
    extra_data: Optional[Dict[str, Any]],
    content: str,
    owner_name: str = "",
    timestamp: Optional[float] = None,
) -> ConversationEnvelope:
    if is_qq_source(source):
        qq_ctx = dict(_qq_context(extra_data))
        if not (qq_ctx.get("user_id") or qq_ctx.get("group_id")):
            # Never let a malformed remote event fall back into the owner's local conversation.
            qq_ctx["user_id"] = "unknown"
        return qq_envelope(qq_ctx, content, timestamp)
    return local_envelope(content, owner_name, timestamp)


def envelope_for_qq_log(data: Dict[str, Any]) -> ConversationEnvelope:
    """Background group messages that did not address the bot."""
    sender = str(data.get("sender") or "")
    return qq_envelope(
        {
            "user_id": data.get("user_id"),
            "group_id": data.get("group_id"),
            "sender_name": sender,
            "is_admin": data.get("is_admin"),
        },
        content=str(data.get("content") or ""),
        timestamp=data.get("timestamp") or time.time(),
    )
