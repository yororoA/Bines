"""Identity resolution and visibility rules.

Rules:
- Local chat and NapCat private chats from identities explicitly linked to the owner share the
  ``owner`` scope (long-term memory and relationship state).
- Every other private chat is its own scope; every group is its own scope.
- A conversation recalls only memories from its own scope. Group chats never see owner memories,
  even when the owner is speaking in the group.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .models import (
    CHANNEL_LOCAL,
    CHANNEL_QQ_GROUP,
    CHANNEL_QQ_PRIVATE,
    LOCAL_CONVERSATION,
    OWNER_ACTOR,
    OWNER_SCOPE,
    VISIBILITY_GROUP,
    VISIBILITY_PRIVATE,
    ConversationEnvelope,
    actor_for_qq,
    group_scope,
    private_scope,
)

IdentityLookup = Callable[[str, str], Optional[str]]

PUBLIC_STATE_KEYS = ("npc_state",)


class ScopeError(ValueError):
    pass


def resolve(envelope: ConversationEnvelope, lookup_identity: IdentityLookup) -> ConversationEnvelope:
    """Return a copy of ``envelope`` with conversation, actor, scope and visibility recomputed."""
    resolved = ConversationEnvelope.from_dict(envelope.to_dict())
    channel = resolved.channel_type
    if channel == CHANNEL_LOCAL:
        resolved.conversation_id = LOCAL_CONVERSATION
        resolved.actor_id = OWNER_ACTOR
        resolved.owner_identity = True
        resolved.scope = OWNER_SCOPE
        resolved.visibility = VISIBILITY_PRIVATE
        return resolved

    user_id = str(resolved.external_user_id or "").strip()
    linked = lookup_identity("qq", user_id) if user_id else None
    resolved.owner_identity = linked == OWNER_ACTOR

    if channel == CHANNEL_QQ_PRIVATE:
        if not user_id:
            raise ScopeError("qq_private envelope requires external_user_id")
        resolved.conversation_id = private_scope(user_id)
        resolved.visibility = VISIBILITY_PRIVATE
        if resolved.owner_identity:
            resolved.actor_id = OWNER_ACTOR
            resolved.scope = OWNER_SCOPE
        else:
            resolved.actor_id = actor_for_qq(user_id)
            resolved.scope = private_scope(user_id)
        return resolved

    if channel == CHANNEL_QQ_GROUP:
        group_id = str(resolved.external_group_id or "").strip()
        if not group_id:
            raise ScopeError("qq_group envelope requires external_group_id")
        resolved.conversation_id = group_scope(group_id)
        resolved.scope = group_scope(group_id)
        resolved.visibility = VISIBILITY_GROUP
        resolved.actor_id = actor_for_qq(user_id) if user_id else "qq:unknown"
        return resolved

    raise ScopeError(f"unknown channel_type: {channel!r}")


def recall_scopes(scope: str) -> List[str]:
    return [scope]


def can_view(record_scope: str, viewer_scope: str) -> bool:
    return record_scope in recall_scopes(viewer_scope)


def visible_state(scope: str, owner_state: Dict[str, Any]) -> Dict[str, Any]:
    """Owner conversations see the full state; others only the character's public status."""
    if scope == OWNER_SCOPE:
        return dict(owner_state)
    return {key: owner_state[key] for key in PUBLIC_STATE_KEYS if key in owner_state}
