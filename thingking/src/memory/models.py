"""Shared data model for the memory system (used by both the Memory Server and its clients)."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

CHANNEL_LOCAL = "local"
CHANNEL_QQ_PRIVATE = "qq_private"
CHANNEL_QQ_GROUP = "qq_group"
CHANNELS = (CHANNEL_LOCAL, CHANNEL_QQ_PRIVATE, CHANNEL_QQ_GROUP)

OWNER_SCOPE = "owner"
OWNER_ACTOR = "owner"
LOCAL_CONVERSATION = "local"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_OBSERVED = "observed"
ROLES = (ROLE_USER, ROLE_ASSISTANT, ROLE_OBSERVED)

KIND_EPISODE = "episode"
KIND_SEMANTIC = "semantic"
KIND_DIARY = "diary"
KIND_EXCERPT = "excerpt"
MEMORY_KINDS = (KIND_EPISODE, KIND_SEMANTIC, KIND_DIARY, KIND_EXCERPT)

ORIGIN_LIVE = "live"
ORIGIN_LEGACY = "legacy"

VISIBILITY_PRIVATE = "private"
VISIBILITY_GROUP = "group"

_SYSTEM_EVENT_PREFIXES = ("[system event:", "[system event :", "[系统：", "[系统:", "[screen monitor")


def is_system_event(text: str) -> bool:
    return (text or "").strip().lower().startswith(_SYSTEM_EVENT_PREFIXES)


def content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def day_key(ts: Optional[float] = None, day_start_hour: int = 4) -> str:
    """Calendar day for memory purposes; the day rolls over at ``day_start_hour`` (default 04:00)."""
    dt = datetime.fromtimestamp(time.time() if ts is None else float(ts))
    if dt.hour < day_start_hour:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def actor_for_qq(user_id: Any) -> str:
    return f"qq:{user_id}"


def group_scope(group_id: Any) -> str:
    return f"{CHANNEL_QQ_GROUP}:{group_id}"


def private_scope(user_id: Any) -> str:
    return f"{CHANNEL_QQ_PRIVATE}:{user_id}"


@dataclass
class ConversationEnvelope:
    """One normalized message context, independent of where it came from.

    ``scope`` and ``owner_identity`` are recomputed by the Memory Server from its identity table;
    values sent by clients are never trusted.
    """

    source: str
    channel_type: str
    conversation_id: str
    actor_id: str
    actor_name: str = ""
    external_user_id: str = ""
    external_group_id: str = ""
    owner_identity: bool = False
    scope: str = ""
    visibility: str = VISIBILITY_PRIVATE
    timestamp: float = field(default_factory=time.time)
    content: str = ""
    is_admin: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationEnvelope":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in (data or {}).items() if key in known})

    @property
    def is_group(self) -> bool:
        return self.channel_type == CHANNEL_QQ_GROUP
