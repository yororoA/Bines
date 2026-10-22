"""Memory lifecycle: record turns, assemble scoped context, run consolidation jobs, admin operations."""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import scope_policy
from .models import (
    KIND_DIARY,
    KIND_EPISODE,
    KIND_EXCERPT,
    KIND_SEMANTIC,
    MEMORY_KINDS,
    ORIGIN_LIVE,
    OWNER_ACTOR,
    OWNER_SCOPE,
    ROLE_ASSISTANT,
    ROLE_OBSERVED,
    ROLE_USER,
    ROLES,
    ConversationEnvelope,
    content_hash,
    day_key,
    is_system_event,
)
from .repository import MemoryRepository
from .settings import MemorySettings
from .vector_index import VectorIndex
from .lexical_index import LexicalIndex

logger = logging.getLogger("bines.memory.service")

JOB_SUMMARIZE = "summarize"
JOB_DIARY = "diary"
JOB_STATE = "update_state"
ASSISTANT_ACTOR = "assistant"
SEMANTIC_DUPLICATE_DISTANCE = 0.08
DIARY_CHECK_INTERVAL = 600
CROSS_CHANNEL_SECONDS = 12 * 3600
CROSS_CHANNEL_LIMIT = 8

_REFERENCE_MARKERS = ("它", "那个", "这个", "那件事", "这件事", "接着", "继续", "然后", "后来", "之前", "刚才")
_DECAY_PER_DAY = {KIND_EPISODE: 0.03, KIND_EXCERPT: 0.03, KIND_DIARY: 0.01, KIND_SEMANTIC: 0.0}
_KIND_WEIGHT = {KIND_EPISODE: 1.0, KIND_EXCERPT: 0.8, KIND_DIARY: 1.0, KIND_SEMANTIC: 1.0}


def _message_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in ("id", "role", "actor_id", "actor_name", "content", "ts")}


def _memory_view(row: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    view = {
        key: row.get(key)
        for key in ("id", "kind", "scope", "conversation_id", "subject_id", "content", "importance",
                    "start_ts", "end_ts", "day_key", "tags", "origin")
    }
    view.update(extra)
    return view


class MemoryService:
    def __init__(
        self,
        repo: MemoryRepository,
        index: VectorIndex,
        settings: MemorySettings,
        clock: Callable[[], float] = time.time,
    ):
        self.repo = repo
        self.index = index
        self.settings = settings
        self.clock = clock
        self._last_diary_check = 0.0

    # ---------- identity ----------
    def sync_identities(self) -> None:
        with self.repo.transaction():
            self.repo.upsert_identity(OWNER_ACTOR, "owner", self.settings.owner_name)
            for qq_id in self.settings.owner_qq_ids:
                self.repo.link_identity("qq", qq_id, OWNER_ACTOR)
            self.repo.unlink_identities("qq", OWNER_ACTOR, self.settings.owner_qq_ids)

    def resolve(self, envelope: Dict[str, Any] | ConversationEnvelope) -> ConversationEnvelope:
        if isinstance(envelope, dict):
            envelope = ConversationEnvelope.from_dict(envelope)
        resolved = scope_policy.resolve(envelope, self.repo.lookup_identity)
        title = resolved.actor_name if resolved.channel_type != "qq_group" else ""
        self.repo.ensure_conversation(resolved.conversation_id, resolved.channel_type, resolved.scope, title)
        return resolved

    def _day(self, ts: Optional[float]) -> str:
        return day_key(ts, self.settings.day_start_hour)

    # ---------- recording ----------
    def record_turn(self, envelope: Dict[str, Any], user_text: str = "", assistant_text: str = "",
                    state_update: bool = True) -> Dict[str, Any]:
        with self.repo.transaction():
            env = self.resolve(envelope)
            ts = float(env.timestamp or self.clock())
            message_ids: List[int] = []
            if user_text.strip() and not is_system_event(user_text):
                message_ids.append(self.repo.add_message(
                    env.conversation_id, env.scope, ROLE_USER, user_text.strip(), ts, self._day(ts),
                    actor_id=env.actor_id, actor_name=env.actor_name,
                ))
            if assistant_text.strip():
                reply_ts = max(ts, self.clock())
                message_ids.append(self.repo.add_message(
                    env.conversation_id, env.scope, ROLE_ASSISTANT, assistant_text.strip(), reply_ts,
                    self._day(reply_ts), actor_id=ASSISTANT_ACTOR,
                ))
            self._maybe_enqueue_summary(env.conversation_id, env.scope)
            if state_update and message_ids and user_text.strip() and not is_system_event(user_text):
                self._enqueue_state(env, message_ids[0])
        return {"conversation_id": env.conversation_id, "scope": env.scope, "message_ids": message_ids}

    def record_message(self, envelope: Dict[str, Any], role: str, content: str,
                       source_ref: Optional[str] = None, recalled: bool = False,
                       state_update: bool = True) -> Dict[str, Any]:
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        if not content.strip():
            return {"message_ids": []}
        with self.repo.transaction():
            env = self.resolve(envelope)
            ts = float(env.timestamp or self.clock())
            actor_id = ASSISTANT_ACTOR if role == ROLE_ASSISTANT else env.actor_id
            if source_ref and recalled:
                old = self.repo.conn.execute(
                    "SELECT id,episode_id FROM messages WHERE source_ref=?", (source_ref,)
                ).fetchone()
                if old:
                    self._cancel_state_for_message(old["id"])
                    self.repo.conn.execute(
                        "UPDATE jobs SET status='done',dedupe_key=NULL WHERE kind=? AND conversation_id=? "
                        "AND status IN ('pending','running')",
                        (JOB_SUMMARIZE, env.conversation_id),
                    )
                    # Invalidate summaries/facts derived from a recalled message.
                    if old["episode_id"]:
                        episode = self.repo.get_memory(old["episode_id"])
                        if episode:
                            derived = self.repo.list_memories(
                                scopes=[env.scope], day_key=episode.get("day_key") or "", limit=100000,
                            )
                            for memory in derived:
                                if (memory["id"] == episode["id"] or memory["kind"] == KIND_DIARY
                                        or memory["meta"].get("episode_id") == episode["id"]):
                                    self.delete_memory(memory["id"])
                        self.repo.conn.execute(
                            "UPDATE messages SET episode_id=NULL WHERE episode_id=?", (old["episode_id"],)
                        )
                    self.repo.conn.execute(
                        "UPDATE messages SET content='[消息已撤回]' WHERE id=?", (old["id"],)
                    )
            message_id = self.repo.add_message(
                env.conversation_id, env.scope, role, content.strip(), ts, self._day(ts),
                actor_id=actor_id, actor_name="" if role == ROLE_ASSISTANT else env.actor_name,
                source_ref=source_ref,
            )
            self._maybe_enqueue_summary(env.conversation_id, env.scope)
            if state_update and role == ROLE_USER and not recalled and not is_system_event(content):
                self._enqueue_state(env, message_id)
        return {"conversation_id": env.conversation_id, "scope": env.scope, "message_ids": [message_id]}

    # ---------- context ----------
    def build_context(self, envelope: Dict[str, Any], query: str = "") -> Dict[str, Any]:
        env = self.resolve(envelope)
        scopes = scope_policy.recall_scopes(env.scope)
        working = self.repo.recent_messages(env.conversation_id, self.settings.working_window)
        window_start = working[0]["ts"] if working else None
        bridge = self.repo.latest_episode_before(env.conversation_id, window_start)

        recall_query = self._recall_query(query or env.content, working)
        excluded = {bridge["id"]} if bridge else set()
        recalled = self._recall(
            recall_query, scopes, [KIND_EPISODE, KIND_EXCERPT, KIND_DIARY], excluded,
            env.conversation_id, window_start,
        )
        diaries = [item for item in recalled if item["kind"] == KIND_DIARY][: self.settings.diary_k]
        episodes = [item for item in recalled if item["kind"] != KIND_DIARY][: self.settings.recall_k]

        other_channels: List[Dict[str, Any]] = []
        if env.scope == OWNER_SCOPE:
            other_channels = [
                {**_message_view(m), "conversation_id": m["conversation_id"]}
                for m in self.repo.recent_scope_messages(
                    env.scope, env.conversation_id, self.clock() - CROSS_CHANNEL_SECONDS, CROSS_CHANNEL_LIMIT
                )
            ]

        owner_state = self.repo.get_state(OWNER_SCOPE)["data"]
        state_scope = f"{env.scope}:user:{env.external_user_id}" if env.is_group else env.scope
        visible_state = scope_policy.visible_state(env.scope, owner_state)
        if env.scope != OWNER_SCOPE:
            visible_state.update(self.repo.get_state(state_scope)["data"])
        last_user = self.repo.last_message(env.conversation_id, ROLE_USER)
        return {
            "envelope": env.to_dict(),
            "scope": env.scope,
            "conversation_id": env.conversation_id,
            "working": [_message_view(m) for m in working],
            "other_channels": other_channels,
            "bridge": _memory_view(bridge) if bridge else None,
            "recalled": episodes + diaries,
            "profile": self._profile(env, scopes, recall_query),
            "state": visible_state,
            "last_user_ts": last_user["ts"] if last_user else None,
        }

    @staticmethod
    def _recall_query(query: str, working: Sequence[Dict[str, Any]]) -> str:
        query = (query or "").strip()
        if any(marker in query for marker in _REFERENCE_MARKERS):
            recent_user = [m["content"] for m in working if m["role"] == ROLE_USER][-2:]
            query = "\n".join([*recent_user, query])
        return query

    def _score(self, row: Dict[str, Any], distance: float, now: float) -> float:
        relevance = max(0.0, 1.0 - distance)
        reference_ts = row.get("end_ts") or row.get("created_at") or now
        age_days = max(0.0, (now - float(reference_ts)) / 86400.0)
        # 久远但相关的回忆不能因年龄被无关的新片段完全挤掉。
        decay = 0.65 + 0.35 * math.exp(-_DECAY_PER_DAY.get(row["kind"], 0.03) * age_days)
        # 旧库曾使用 0–100 分制，不能让 importance=100 永久压过真正相关的记忆。
        importance = 0.7 + self._clamp_importance(row.get("importance") or 5) / 20.0
        return relevance * decay * importance * _KIND_WEIGHT.get(row["kind"], 1.0)

    def _recall(
        self,
        query: str,
        scopes: Sequence[str],
        kinds: Sequence[str],
        excluded: Iterable[str],
        conversation_id: str,
        window_start: Optional[float],
    ) -> List[Dict[str, Any]]:
        if not query:
            return []
        count = (self.settings.recall_k + self.settings.diary_k) * 4
        try:
            hits = self.index.query(query, scopes, kinds, n=count)
        except Exception:
            logger.exception("vector recall failed")
            hits = []
        # 中文名称、日期和专有名词同时用词面匹配，向量服务异常也能继续召回。
        distances = dict(hits)
        for memory_id, distance in LexicalIndex(self.repo).query(query, scopes, kinds, n=count):
            distances[memory_id] = min(distances.get(memory_id, 1.0), distance)
        hits = list(distances.items())
        rows = self.repo.get_memories([memory_id for memory_id, _ in hits])
        excluded = set(excluded)
        now = self.clock()
        scored = []
        for memory_id, distance in hits:
            row = rows.get(memory_id)
            if not row or memory_id in excluded or not scope_policy.can_view(row["scope"], scopes[0]):
                continue
            overlaps_window = (
                row["kind"] == KIND_EPISODE
                and row.get("conversation_id") == conversation_id
                and window_start is not None
                and (row.get("end_ts") or 0) >= window_start
            )
            if overlaps_window:
                continue
            scored.append(_memory_view(row, score=round(self._score(row, distance, now), 4)))
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored

    def _profile(self, env: ConversationEnvelope, scopes: Sequence[str], query: str) -> List[Dict[str, Any]]:
        subject = OWNER_ACTOR if env.scope == OWNER_SCOPE else env.actor_id
        facts = self.repo.list_memories(
            kinds=[KIND_SEMANTIC], scopes=scopes, subject_id=subject, order="importance",
            limit=max(32, self.settings.profile_k),
        )
        # 原 permanent 用户画像很小且每条都重要，完整提供而非随机丢弃第九条以后。
        # 关键词召回优先放在画像开头，固定画像保留；规模增长时也不会挤掉相关事实。
        relevant = self._recall(query, scopes, [KIND_SEMANTIC], (), env.conversation_id, None)
        selected = relevant[:self.settings.profile_k]
        selected_ids = {item["id"] for item in selected}
        facts = selected + [item for item in facts if item["id"] not in selected_ids]
        return [_memory_view(fact) for fact in facts]

    # ---------- state ----------
    def get_state(self, scope: str = OWNER_SCOPE) -> Dict[str, Any]:
        return self.repo.get_state(scope)

    def put_state(self, scope: str, data: Dict[str, Any], expected_version: Optional[int] = None,
                  source_ref: Optional[str] = None) -> Dict[str, Any]:
        with self.repo.transaction():
            version = self.repo.put_state(scope, data, expected_version)
            if source_ref:
                message = self.repo.conn.execute(
                    "SELECT id FROM messages WHERE source_ref=?", (source_ref,),
                ).fetchone()
                if message:
                    self._cancel_state_for_message(message["id"])
        return {"version": version}

    def import_gateway_snapshot(self, source: str, states: Sequence[Dict[str, Any]],
                                memories: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Idempotent import of the former gateway state and layered-memory rows."""
        imported = 0
        with self.repo.transaction():
            for row in states:
                key = "state:" + row["scope"]
                if self.repo.ledger_get(source, key):
                    continue
                data = dict(row["data"])
                data.pop("_version", None)
                current = self.repo.get_state(row["scope"])
                self.repo.put_state(row["scope"], {**data, **current["data"]}, current["version"])
                self.repo.ledger_add(source, key, "state", row["scope"], content_hash(str(data)))
                imported += 1
            for row in memories:
                key = "memory:" + row["id"]
                if self.repo.ledger_get(source, key):
                    continue
                metadata = row.get("metadata") or {}
                kind = {"episodic": KIND_EPISODE, "semantic": KIND_SEMANTIC, "profile": KIND_SEMANTIC,
                        "diary": KIND_DIARY, "long_term_diary": KIND_DIARY,
                        "summary_buffer": KIND_EPISODE}.get(row["collection"], KIND_EXCERPT)
                if metadata.get("type") == "episode_summary":
                    kind = KIND_EPISODE
                memory_id = self.repo.add_memory(
                    kind, row["scope"], row["content"], meta=metadata,
                    source_ref=source + ":" + key, end_ts=row.get("ts"), origin="legacy",
                    day_key=self._day(row.get("ts")),
                    subject_id=OWNER_ACTOR if kind == KIND_SEMANTIC and row["scope"] == OWNER_SCOPE else None,
                )
                self.repo.ledger_add(source, key, "memories", memory_id, content_hash(row["content"]))
                imported += 1
        self.reconcile_index()
        return {"imported": imported}

    def activity_snapshot(self, limit: int = 10) -> Dict[str, Any]:
        """Owner's local conversation tail and state, for proactive behaviour (bored detector)."""
        messages = self.repo.recent_messages("local", limit)
        return {"messages": [_message_view(m) for m in messages], "state": self.repo.get_state(OWNER_SCOPE)["data"]}

    # ---------- consolidation jobs ----------
    def _cancel_state_for_message(self, message_id: int) -> None:
        self.repo.conn.execute(
            "UPDATE jobs SET status='done',lease_until=NULL,updated_at=? "
            "WHERE kind=? AND dedupe_key=? AND status IN ('pending','running')",
            (self.clock(), JOB_STATE, f"{JOB_STATE}:{message_id}"),
        )

    def _enqueue_state(self, env: ConversationEnvelope, message_id: int) -> None:
        if not self.settings.state_updates:
            return
        scope = f"{env.scope}:user:{env.external_user_id}" if env.is_group else env.scope
        self.repo.enqueue_job(
            JOB_STATE, scope, {"message_id": message_id, "memory_scope": env.scope},
            dedupe_key=f"{JOB_STATE}:{message_id}", conversation_id=env.conversation_id,
        )

    def _maybe_enqueue_summary(self, conversation_id: str, scope: str) -> None:
        if self.repo.has_open_job(JOB_SUMMARIZE, conversation_id):
            return
        if self.repo.pending_message_count(conversation_id) < self.settings.summarize_trigger:
            return
        chunk = self.repo.oldest_pending_messages(conversation_id, self.settings.summarize_chunk)
        if not chunk:
            return
        from_id, to_id = chunk[0]["id"], chunk[-1]["id"]
        self.repo.enqueue_job(
            JOB_SUMMARIZE, scope, {"from_id": from_id, "to_id": to_id},
            dedupe_key=f"{JOB_SUMMARIZE}:{conversation_id}:{from_id}:{to_id}", conversation_id=conversation_id,
        )

    def enqueue_due_diaries(self) -> int:
        today = self._day(self.clock())
        created = 0
        for day in self.repo.undiaried_live_days(OWNER_SCOPE, today):
            if self.repo.enqueue_job(JOB_DIARY, OWNER_SCOPE, {"day_key": day}, dedupe_key=f"{JOB_DIARY}:{OWNER_SCOPE}:{day}"):
                created += 1
        return created

    def claim_job(self, kinds: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
        now = self.clock()
        if now - self._last_diary_check >= DIARY_CHECK_INTERVAL:
            self._last_diary_check = now
            self.enqueue_due_diaries()
        while True:
            job = self.repo.claim_job(self.settings.job_lease_seconds, kinds=kinds or [JOB_SUMMARIZE, JOB_DIARY])
            if job is None:
                return None
            materials = self._job_materials(job)
            if materials is not None:
                return {"job": job, "materials": materials}
            self.repo.finish_job(job["id"])

    def _job_materials(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if job["kind"] == JOB_STATE:
            message = self.repo.get_message(int(job["payload"]["message_id"]))
            if not message or message["content"] == "[消息已撤回]":
                return None
            state = self.repo.get_state(job["scope"])
            # 后台判断逐条使用真实消息；不等待 40 条摘要，也不把其他群成员混入个人状态。
            return {
                "scope": job["scope"], "messages": [_message_view(message)],
                "state": state["data"], "version": state["version"],
            }
        if job["kind"] == JOB_SUMMARIZE:
            payload = job["payload"]
            messages = [
                m for m in self.repo.messages_in_range(job["conversation_id"], payload["from_id"], payload["to_id"])
                if m["episode_id"] is None
            ]
            if not messages:
                return None
            return {
                "scope": job["scope"],
                "conversation_id": job["conversation_id"],
                "messages": [_message_view(m) for m in messages],
            }
        if job["kind"] == JOB_DIARY:
            day = job["payload"]["day_key"]
            if self.repo.list_memories(kinds=[KIND_DIARY], scopes=[job["scope"]], day_key=day, limit=1):
                return None
            episodes = self.repo.episodes_for_day(job["scope"], day)
            if not episodes:
                return None
            return {
                "scope": job["scope"],
                "day_key": day,
                "episodes": [e["content"] for e in episodes],
                "state": self.repo.get_state(OWNER_SCOPE)["data"],
            }
        return None

    def complete_state_update(self, job_id: int, changes: Dict[str, Any],
                              expected_version: Optional[int] = None) -> Dict[str, Any]:
        """按消息去重，状态增量和任务完成在同一事务中提交，重试不重复加好感度。"""
        from tools.memory_tool import _apply_changes
        import inspect

        with self.repo.transaction():
            job = self._running_job(job_id, JOB_STATE)
            if job is None:
                return {"status": "ignored"}
            if not isinstance(changes, dict):
                raise ValueError("changes 必须是对象")
            allowed = set(inspect.signature(_apply_changes).parameters) - {"state"}
            if set(changes) - allowed:
                raise ValueError("状态更新包含未知字段")
            changes = {key: value for key, value in changes.items() if value is not None}
            delta = changes.get("relationship_delta")
            if delta is not None and (type(delta) is not int or not -2 <= delta <= 2):
                raise ValueError("自动 relationship_delta 必须为 -2 到 2 的整数")
            if any(not isinstance(value, str) for key, value in changes.items() if key != "relationship_delta"):
                raise ValueError("状态文本字段必须是字符串")
            if delta == 0:
                changes.pop("relationship_delta", None)
            if changes:
                current = self.repo.get_state(job["scope"])
                updated = _apply_changes(current["data"], **changes)
                if updated != current["data"]:
                    self.repo.put_state(job["scope"], updated,
                                        current["version"] if expected_version is None else expected_version)
            self.repo.finish_job(job["id"])
        return {"status": "done", "scope": job["scope"], "changed": bool(changes)}

    def _running_job(self, job_id: int, kind: str) -> Optional[Dict[str, Any]]:
        job = self.repo.get_job(int(job_id))
        if not job or job["kind"] != kind or job["status"] != "running":
            return None
        return job

    def complete_summary(
        self,
        job_id: int,
        summary: str,
        tags: Sequence[str] = (),
        facts: Sequence[Dict[str, Any]] = (),
        importance: float = 5.0,
    ) -> Dict[str, Any]:
        job = self._running_job(job_id, JOB_SUMMARIZE)
        if job is None:
            return {"status": "ignored"}
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("summary is empty")
        conversation_id, scope = job["conversation_id"], job["scope"]
        payload = job["payload"]
        fresh_facts = self._fresh_facts(scope, facts)
        new_ids: List[str] = []
        with self.repo.transaction():
            messages = [
                m for m in self.repo.messages_in_range(conversation_id, payload["from_id"], payload["to_id"])
                if m["episode_id"] is None
            ]
            if not messages:
                self.repo.finish_job(job["id"])
                return {"status": "done", "episode_id": None, "scope": scope}
            end_ts = max(m["ts"] for m in messages)
            episode_id = self.repo.add_memory(
                KIND_EPISODE, scope, summary, conversation_id=conversation_id,
                importance=self._clamp_importance(importance), start_ts=min(m["ts"] for m in messages),
                end_ts=end_ts, day_key=self._day(end_ts), tags=[str(t) for t in tags][:8],
                meta={"message_range": [payload["from_id"], payload["to_id"]]},
            )
            new_ids.append(episode_id)
            self.repo.mark_consolidated(conversation_id, payload["from_id"], payload["to_id"], episode_id)
            actors = {m["actor_id"] for m in messages if m["role"] in (ROLE_USER, ROLE_OBSERVED)}
            for fact in fresh_facts:
                subject = str(fact.get("subject") or "").strip()
                if scope == OWNER_SCOPE and subject in ("", "user"):
                    subject = OWNER_ACTOR
                new_ids.append(self.repo.add_memory(
                    KIND_SEMANTIC, scope, fact["content"], conversation_id=conversation_id,
                    subject_id=subject if subject in actors else None,
                    importance=self._clamp_importance(fact.get("importance", 6)), end_ts=end_ts,
                    day_key=self._day(end_ts), meta={"episode_id": episode_id},
                ))
            self.repo.finish_job(job["id"])
            self._maybe_enqueue_summary(conversation_id, scope)
        self._index_ids(new_ids)
        return {"status": "done", "episode_id": episode_id, "semantic_ids": new_ids[1:], "scope": scope}

    def _fresh_facts(self, scope: str, facts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fresh = []
        for fact in facts or ():
            text = str((fact or {}).get("content") or "").strip()
            if not text:
                continue
            existing = self.repo.find_memory_by_hash(scope, KIND_SEMANTIC, content_hash(text), fact.get("subject"))
            if existing is None:
                try:
                    near = self.index.query(text, [scope], [KIND_SEMANTIC], n=1)
                except Exception:
                    near = []
                if near and near[0][1] < SEMANTIC_DUPLICATE_DISTANCE:
                    existing = near[0][0]
            if existing:
                self.repo.touch_memory(existing)
                continue
            fresh.append({**fact, "content": text})
        return fresh

    def complete_diary(self, job_id: int, text: str) -> Dict[str, Any]:
        job = self._running_job(job_id, JOB_DIARY)
        if job is None:
            return {"status": "ignored"}
        text = (text or "").strip()
        if not text:
            raise ValueError("diary is empty")
        day = job["payload"]["day_key"]
        with self.repo.transaction():
            diary_id = None
            if not self.repo.list_memories(kinds=[KIND_DIARY], scopes=[job["scope"]], day_key=day, limit=1):
                diary_id = self.repo.add_memory(KIND_DIARY, job["scope"], text, importance=8, day_key=day)
            self.repo.finish_job(job["id"])
        if diary_id:
            self._index_ids([diary_id])
        return {"status": "done", "diary_id": diary_id}

    def fail_job(self, job_id: int, error: str) -> Dict[str, Any]:
        return {"status": self.repo.fail_job(int(job_id), error or "unknown error", self.settings.job_max_attempts)}

    @staticmethod
    def _clamp_importance(value: Any) -> float:
        try:
            return max(0.0, min(10.0, float(value)))
        except (TypeError, ValueError):
            return 5.0

    # ---------- index maintenance ----------
    def _index_ids(self, memory_ids: Sequence[str]) -> None:
        rows = list(self.repo.get_memories(memory_ids).values())
        self._index_rows(rows)

    def _index_rows(self, rows: Sequence[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        if not getattr(self.index, "persistent", True):
            return 0  # Leave Chroma reconciliation marks intact across runtime profiles.
        try:
            indexed = self.index.upsert(rows)
        except Exception:
            logger.exception("indexing %d memories failed; they stay queued for reconcile", len(rows))
            return 0
        self.repo.mark_indexed(indexed)
        return len(indexed)

    def reconcile_index(self) -> Dict[str, int]:
        if not getattr(self.index, "persistent", True):
            return {"indexed": 0, "removed": 0}
        indexed = 0
        while True:
            batch = self.repo.unindexed_memories()
            if not batch:
                break
            done = self._index_rows(batch)
            if done == 0:
                break
            indexed += done
        removed = self.repo.deleted_indexed_memory_ids()
        if removed:
            self.index.delete(removed)
            self.repo.mark_indexed(removed, indexed=False)
        return {"indexed": indexed, "removed": len(removed)}

    def rebuild_index(self) -> Dict[str, int]:
        if not getattr(self.index, "persistent", True):
            return {"indexed": 0, "removed": 0, "total": self.index.count()}
        self.index.reset()
        self.repo.clear_index_marks()
        result = self.reconcile_index()
        result["total"] = self.index.count()
        return result

    # ---------- admin ----------
    def stats(self) -> Dict[str, Any]:
        stats = self.repo.stats()
        stats["index_count"] = self.index.count()
        stats["identity_links"] = self.repo.identity_links()
        stats["conversations"] = self.repo.list_conversations()
        return stats

    def list_memories(self, **filters: Any) -> Dict[str, Any]:
        page_filters = {k: filters.get(k) for k in ("kinds", "scopes", "subject_id", "keyword", "day_key")}
        items = self.repo.list_memories(
            **{k: v for k, v in page_filters.items() if v},
            offset=int(filters.get("offset") or 0), limit=min(int(filters.get("limit") or 50), 500),
            order=filters.get("order") or "recent",
        )
        return {"items": items, "total": self.repo.count_memories(**{k: v for k, v in page_filters.items() if v})}

    def search(self, query: str, scopes: Sequence[str], kinds: Optional[Sequence[str]] = None, k: int = 10) -> List[Dict[str, Any]]:
        try:
            hits = dict(self.index.query(query, scopes, kinds or MEMORY_KINDS, n=k * 4))
        except Exception:
            logger.exception("vector search failed")
            hits = {}
        for memory_id, distance in LexicalIndex(self.repo).query(query, scopes, kinds or MEMORY_KINDS, n=k * 4):
            hits[memory_id] = min(hits.get(memory_id, 1.0), distance)
        rows = self.repo.get_memories(list(hits))
        return [
            {**rows[memory_id], "distance": round(distance, 4)}
            for memory_id, distance in sorted(hits.items(), key=lambda item: item[1])
            if memory_id in rows and rows[memory_id]["scope"] in scopes
        ][:k]

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        return self.repo.get_memory(memory_id)

    def add_memory(self, kind: str, scope: str, content: str, **fields: Any) -> Dict[str, Any]:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown kind: {kind}")
        if not content.strip():
            raise ValueError("content is empty")
        memory_id = self.repo.add_memory(
            kind, scope, content.strip(), subject_id=fields.get("subject_id") or None,
            importance=self._clamp_importance(fields.get("importance", 5)),
            day_key=fields.get("day_key") or self._day(self.clock()), tags=fields.get("tags") or (),
            end_ts=self.clock(), origin=ORIGIN_LIVE,
        )
        self._index_ids([memory_id])
        return {"id": memory_id}

    def update_memory(self, memory_id: str, **fields: Any) -> Dict[str, Any]:
        if "importance" in fields and fields["importance"] is not None:
            fields["importance"] = self._clamp_importance(fields["importance"])
        updated = self.repo.update_memory(memory_id, **fields)
        if updated:
            self._index_ids([memory_id])
        return {"updated": updated}

    def delete_memory(self, memory_id: str) -> Dict[str, Any]:
        deleted = self.repo.delete_memory(memory_id)
        if deleted and getattr(self.index, "persistent", True):
            try:
                self.index.delete([memory_id])
                self.repo.mark_indexed([memory_id], indexed=False)
            except Exception:
                logger.exception("index delete failed; reconcile will retry")
        return {"deleted": deleted}

    def list_conversations(self) -> List[Dict[str, Any]]:
        return self.repo.list_conversations()

    def conversation_messages(self, conversation_id: str, offset: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        return self.repo.messages_page(conversation_id, int(offset), min(int(limit), 500))

    def export(self, scopes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        memories: List[Dict[str, Any]] = []
        for batch in self.repo.all_active_memories():
            memories.extend(m for m in batch if not scopes or m["scope"] in scopes)
        return {"memories": memories, "state": self.repo.get_state(OWNER_SCOPE), "exported_at": self.clock()}

    def ping(self) -> Dict[str, Any]:
        return {"ok": True, "index_count": self.index.count()}
