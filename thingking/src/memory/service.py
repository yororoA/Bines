"""Memory lifecycle: record turns, assemble scoped context, run consolidation jobs, admin operations."""
from __future__ import annotations

import hashlib
import copy
import json
import logging
import math
import time
from datetime import datetime, timedelta
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
    ZONE,
    ConversationEnvelope,
    content_hash,
    day_key,
    is_system_event,
)
from .repository import MemoryRepository, consolidation_candidate
from .settings import MemorySettings
from .vector_index import VectorIndex
from .temporal import TemporalStore
from .retrieval import HybridRetriever, time_intent
from .life import LifeLedger
from .associations import AssociationStore, JOB_ASSOCIATE, digest
from .retention import RetentionStore
from .usage import UsageStore
from .evidence import _chunks, _tokens

logger = logging.getLogger("bines.memory.service")

JOB_SUMMARIZE = "summarize"
JOB_DIARY = "diary"
JOB_STATE = "update_state"
ASSISTANT_ACTOR = "assistant"
SEMANTIC_DUPLICATE_DISTANCE = 0.08
DIARY_CHECK_INTERVAL = 600
INDEX_CHECK_INTERVAL = 5
CROSS_CHANNEL_SECONDS = 12 * 3600
CROSS_CHANNEL_LIMIT = 8

_REFERENCE_MARKERS = ("它", "那个", "这个", "那件事", "这件事", "接着", "继续", "然后", "后来", "之前", "刚才")
_DECAY_PER_DAY = {KIND_EPISODE: 0.03, KIND_EXCERPT: 0.03, KIND_DIARY: 0.01, KIND_SEMANTIC: 0.0}
_KIND_WEIGHT = {KIND_EPISODE: 1.0, KIND_EXCERPT: 0.8, KIND_DIARY: 1.0, KIND_SEMANTIC: 1.0}


def _association_excerpt(text: str, query: str, limit: int = 1400) -> str:
    if len(text) <= limit:
        return text
    terms = set(_tokens(query))
    selected = max(_chunks(text), key=lambda part: len(terms.intersection(_tokens(part))), default="")
    position = text.find(selected) if selected else 0
    head = min(300, limit // 4)
    if position < head:
        return text[:limit]
    separator = "\n[与本次建联查询相关的原文片段]\n"
    span_size = limit - head - len(separator)
    start = max(0, min(position - 150, len(text) - span_size))
    # Both spans remain verbatim; the separator cannot be quoted as source evidence.
    return text[:head] + separator + text[start:start + span_size]


def _message_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in ("id", "role", "actor_id", "actor_name", "content", "ts")}


def _memory_view(row: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    view = {
        key: row.get(key)
        for key in ("id", "kind", "scope", "conversation_id", "subject_id", "content", "importance",
                    "start_ts", "end_ts", "day_key", "tags", "meta", "origin", "evidence", "key_id",
                    "retrieval_channels", "score", "memory_id", "message_id")
    }
    for key in ("node_id", "citation_id", "node_digest", "source_family", "chain_ids", "associations", "retrieval_status",
                "context_id", "reason_shares", "corrections", "evidence_family", "matched_spans", "temporal",
                "retrieval_role", "graph_activation"):
        if key in row:
            view[key] = row[key]
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
        self._last_index_check = float("-inf")
        from .dependencies import SourceDependencies
        from .diary_queue import DiaryQueue
        self.dependencies = self.repo.dependencies = SourceDependencies(repo)
        self.diary_queue = DiaryQueue(repo)
        # 时态历史与检索键都是派生层：服务显式挂载，独立仓库使用时不强制迁移。
        self.repo.temporal = TemporalStore(repo)
        from .state_effects import StateEffects
        self.state_effects = StateEffects(repo)
        self.retriever = HybridRetriever(repo, index, settings)
        self.repo.conn.execute("""CREATE TABLE IF NOT EXISTS experiences (
            source_ref TEXT PRIMARY KEY, memory_id TEXT NOT NULL, scope TEXT NOT NULL,
            record TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at REAL NOT NULL
        )""")
        self.dependencies.ensure_tracking()
        self.life = LifeLedger(repo, clock)
        from .provenance import ProvenanceStore
        from .competition import CompetitionStore
        from .corrections import CorrectionStore
        self.provenance = self.repo.provenance = ProvenanceStore(repo)
        self.graph = self.repo.associations = AssociationStore(repo, settings)
        self.competition = self.repo.competition = CompetitionStore(repo)
        self.corrections = self.repo.corrections = CorrectionStore(self)
        self.usage = self.repo.usage = UsageStore(repo, self.graph)
        self.retention = self.repo.retention = RetentionStore(repo, self.graph, settings)
        from .association_plan import AssociationPlanner
        self.association_planner = AssociationPlanner(self)
        from .maintenance import MemoryMaintenance
        self.maintenance = MemoryMaintenance(self)
        from .costs import ModelCostStore
        self.model_costs = ModelCostStore(repo)
        from .turn_audit import TurnAudit
        self.turn_audit = TurnAudit(self)
        with self.repo.transaction():
            # Existing unsummarized receipts must settle before association.
            for row in self.repo.conn.execute("""SELECT DISTINCT e.memory_id FROM experiences e JOIN jobs j
                ON json_extract(j.payload,'$.source_ref')=e.source_ref
                AND json_extract(j.payload,'$.revision')=e.revision
                WHERE j.kind='summarize_experience' AND j.status IN ('pending','running')""").fetchall():
                memory = self.repo.get_memory(row["memory_id"])
                if memory and not memory["deleted"] and not memory["meta"].get("experience_summary_pending"):
                    self.repo.update_memory(memory["id"], meta={**memory["meta"], "experience_summary_pending": True})
                    self.graph.register_memory(memory["id"])
            self.graph.backfill(limit=settings.association_backfill_batch)

    # ---------- identity ----------
    def sync_identities(self) -> None:
        with self.repo.transaction():
            self.repo.upsert_identity(OWNER_ACTOR, "owner", self.settings.owner_name)
            for qq_id in self.settings.owner_qq_ids:
                self.repo.link_identity("qq", qq_id, OWNER_ACTOR)
            self.repo.unlink_identities("qq", OWNER_ACTOR, self.settings.owner_qq_ids)
            for row in self.repo.conn.execute("SELECT id FROM conversations WHERE channel='qq_private'").fetchall():
                envelope = scope_policy.private_conversation_envelope(row["id"])
                if envelope:
                    # Fence background jobs at binding time, even if this user
                    # never sends another message after being unlinked.
                    self.resolve(envelope)

    def resolve(self, envelope: Dict[str, Any] | ConversationEnvelope) -> ConversationEnvelope:
        if isinstance(envelope, dict):
            envelope = ConversationEnvelope.from_dict(envelope)
        resolved = scope_policy.resolve(envelope, self.repo.lookup_identity)
        title = resolved.actor_name if resolved.channel_type != "qq_group" else ""
        with self.repo.transaction():
            previous = self.repo.conn.execute("SELECT scope FROM conversations WHERE id=?", (resolved.conversation_id,)).fetchone()
            if previous and previous["scope"] != resolved.scope:
                from .identity_migration import migrate
                migrate(self, resolved, previous["scope"])
            self.repo.ensure_conversation(resolved.conversation_id, resolved.channel_type, resolved.scope, title)
        return resolved

    def _day(self, ts: Optional[float]) -> str:
        return day_key(ts, self.settings.day_start_hour)

    def _past_day_bounds(self, day: str) -> Optional[tuple[float, float]]:
        try:
            start = datetime.strptime(day, "%Y-%m-%d")
        except (TypeError, ValueError):
            return None
        if start.strftime("%Y-%m-%d") != day or day >= self._day(self.clock()):
            return None
        start = start.replace(hour=self.settings.day_start_hour, tzinfo=ZONE)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    # ---------- recording ----------
    def record_experience(self, envelope: Dict[str, Any], source_ref: str, record: Dict[str, Any],
                          revision: int = 1) -> Dict[str, Any]:
        from .experience import normalize, summary, urls
        record = normalize(record)
        if not source_ref or type(revision) is not int or revision < 1:
            raise ValueError("经历需要稳定来源和正整数 revision")
        with self.repo.transaction():
            env = self.resolve(envelope)
            old = self.repo.conn.execute("SELECT * FROM experiences WHERE source_ref=?", (source_ref,)).fetchone()
            if old:
                if old["scope"] != env.scope:
                    raise ValueError("经历来源不可跨作用域重用")
                if old["revision"] >= revision:
                    return {"id": old["memory_id"], "status": "duplicate"}
                if self.repo.get_memory(old["memory_id"])["deleted"]:
                    return {"id": old["memory_id"], "status": "deleted"}
            ts = float(record.get("finished_at") or record.get("started_at") or env.timestamp or self.clock())
            if not math.isfinite(ts):
                raise ValueError("经历时间必须有限")
            action = record.get("action") or ""
            # Retrieval itself is retained as an audit, without duplicating the
            # returned old memories as new knowledge on every recall.
            turn_audit = (record.get("category") == "turn"
                          and (not record.get("result") or record["status"] in {"completed", "sent"}
                               and bool(record.get("source_message_refs"))))
            audit_only = turn_audit or action in {"search_memory", "cite_memory", "correct_memory", "get_time", "update_status", "napcat_discover",
                                    "napcat_catalog", "mcp_discover"}
            meta = {"experience_ref": source_ref, "experience_revision": revision,
                    "source_experience_refs": [source_ref], "turn_id": record.get("turn_id"),
                    "autonomous": bool(record.get("autonomous")), "action": action,
                    "status": record["status"], "urls": urls(record), "audit_only": audit_only,
                    "experience_summary_pending": record["status"] != "started" and not audit_only,
                    "retrieval": {"keywords": [action, (record.get("candidate") or {}).get("topic", "")]},
                    "source_message_ids": [], "source_message_refs": record.get("source_message_refs") or []}
            for ref in record.get("source_message_refs") or []:
                message = self.repo.conn.execute("SELECT id,origin FROM messages WHERE source_ref=? AND scope=?",
                                                 (ref, env.scope)).fetchone()
                if message:
                    if message["origin"] in {"recalled", "withdrawn", "deleted", "retracted"}:
                        return {"status": "retracted"}
                    meta["source_message_ids"].append(message["id"])
            content = summary(record)
            if old:
                memory_id = old["memory_id"]
                self.repo.update_memory(memory_id, content=content, meta=meta)
                self.repo.conn.execute("UPDATE memories SET end_ts=? WHERE id=?", (ts, memory_id))
            else:
                memory_id = self.repo.add_memory(
                    KIND_EPISODE, env.scope, content, conversation_id=env.conversation_id,
                    origin="experience", importance=4 if record["status"] in {"failed", "started"} else 5,
                    start_ts=float(record.get("started_at") or ts), end_ts=ts, day_key=self._day(ts),
                    meta=meta, source_ref=source_ref,
                )
            self.repo.conn.execute("""INSERT INTO experiences VALUES(?,?,?,?,?,?)
                ON CONFLICT(source_ref) DO UPDATE SET record=excluded.record,revision=excluded.revision""",
                (source_ref, memory_id, env.scope, json.dumps(record, ensure_ascii=False), revision, self.clock()))
            self.provenance.register(env.scope, source_ref, record)
            self.life.record_experience(env.scope, source_ref, record, revision)
            if record.get("category") == "turn" and record["status"] != "started":
                refs = record.get("turn_message_refs", record.get("source_message_refs") or [])
                messages = []
                for ref in refs:
                    row = self.repo.conn.execute("SELECT id FROM messages WHERE scope=? AND source_ref=?",
                                                 (env.scope, ref)).fetchone()
                    if row:
                        messages.append(self.repo.get_message(row[0]))
                messages = [m for m in messages if m and consolidation_candidate(m["role"], m["content"], m["origin"])]
                node = self.graph.register_raw(messages, source_ref=source_ref)
                if node:
                    self.repo.conn.execute("UPDATE association_nodes SET waiting_until=0 WHERE id=?", (node["id"],))
                if not audit_only:
                    self.graph.register_memory(memory_id)
                feedback = record.get("memory_usage") or {}
                self.usage.record(env.scope, source_ref, feedback.get("presented") or [],
                                  feedback.get("citations") or [], record.get("result") or "")
                self._commit_turn_corrections(env, source_ref, record, feedback)
                self.turn_audit.enqueue(env, source_ref, revision, record, feedback)
                used = [r[0] for r in self.repo.conn.execute(
                    "SELECT node_id FROM memory_usage_events WHERE scope=? AND turn_id=? AND event_type='cited'",
                    (env.scope, source_ref))]
                self.retention.enforce_budget(env.scope, eligible=used)
            elif not audit_only and record["status"] != "started":
                self.graph.register_memory(memory_id)
            if record["status"] != "started" and not audit_only:
                self.repo.enqueue_job("summarize_experience", env.scope,
                    {"source_ref": source_ref, "revision": revision},
                    dedupe_key=f"experience:{source_ref}:{revision}", conversation_id=env.conversation_id)
        # Indexing is recoverable and asynchronous; lexical keys work immediately.
        self.retriever.sync()
        return {"id": memory_id, "status": "recorded"}

    def _commit_turn_corrections(self, env, source_ref, record, feedback):
        proposals = record.get("memory_corrections") or []
        if not proposals or record["status"] not in {"completed", "sent"} or record.get("autonomous"):
            return
        if env.scope != OWNER_SCOPE or env.actor_id != OWNER_ACTOR:
            return
        user_messages = [m["content"] for m in self.turn_audit.user_messages(env, source_ref, record)]
        shown = {item.get("node_id"): item for item in feedback.get("presented") or [] if isinstance(item, dict)}
        outcomes = []
        for proposal in proposals[:5]:
            try:
                if not isinstance(proposal, dict):
                    raise ValueError("纠错提案必须是对象")
                evidence = proposal.get("correction_quote")
                presentation = shown.get(proposal.get("node_id")) or {}
                if (not evidence or not any(evidence in text for text in user_messages)
                        or not proposal.get("replacement") or proposal["replacement"] not in evidence
                        or not proposal.get("quote")
                        or not any(proposal["quote"] in span for span in presentation.get("spans") or [])
                        or presentation.get("node_digest") != proposal.get("expected_digest")):
                    raise ValueError("纠错提案缺少本轮真实用户原话或已呈现的来源")
                params = {key: proposal.get(key) for key in
                          ("node_id", "operation_id", "expected_digest", "quote", "replacement", "explanation")}
                outcomes.append(self.corrections.apply(scope=env.scope, kind="fact", **params))
            except (ValueError, PermissionError, TypeError) as exc:
                outcomes.append({"status": "rejected", "error": str(exc)})
        record["correction_results"] = outcomes
        self.repo.conn.execute("UPDATE experiences SET record=? WHERE source_ref=? AND scope=?",
                              (json.dumps(record, ensure_ascii=False), source_ref, env.scope))

    def complete_experience_summary(self, job_id: int, summary: str, tags: Sequence[str] = (),
                                    expected_attempt=None) -> Dict[str, Any]:
        job = self._running_job(job_id, "summarize_experience", expected_attempt)
        if not job:
            return {"status": "ignored"}
        if not summary.strip():
            raise ValueError("经历摘要不能为空")
        with self.repo.transaction():
            row = self.repo.conn.execute("SELECT * FROM experiences WHERE source_ref=?",
                                        (job["payload"]["source_ref"],)).fetchone()
            memory = self.repo.get_memory(row["memory_id"]) if row else None
            if not row or row["revision"] != job["payload"]["revision"] or memory["deleted"]:
                self.repo.finish_job(job_id)
                return {"status": "ignored"}
            if (job["payload"].get("target_digest") is not None
                    and job["payload"]["target_digest"] != digest([memory["content"], memory["meta"]])):
                self.repo.conn.execute("UPDATE jobs SET status='pending',lease_until=NULL WHERE id=?", (job_id,))
                return {"status": "stale"}
            from .corrections import validate_corrected_text
            node = self.graph.for_memory(memory["id"])
            constraints = self.corrections.inherited_facts(node) if node else []
            validate_corrected_text(summary, constraints)
            record = json.loads(row["record"])
            # Action/status remain machine-supplied even if a model misstates the receipt.
            content = f"Bines经历 · {record.get('action')} · 状态={record['status']}\n{summary.strip()[:2400]}"
            meta = {**memory["meta"], "experience_summary_pending": False}
            self.repo.update_memory(memory["id"], content=content, tags=list(tags)[:5], meta=meta)
            self.graph.register_memory(memory["id"])
            self.repo.finish_job(job_id)
            self.enqueue_due_diaries()
        self._index_ids([memory["id"]])
        return {"status": "done", "id": memory["id"]}

    def record_turn(self, envelope: Dict[str, Any], user_text: str = "", assistant_text: str = "",
                    state_update: bool = True) -> Dict[str, Any]:
        with self.repo.transaction():
            env = self.resolve(envelope)
            ts = float(env.timestamp or self.clock())
            message_ids: List[int] = []
            if user_text.strip() and not is_system_event(user_text):
                message_id = self.repo.add_message(
                    env.conversation_id, env.scope, ROLE_USER, user_text.strip(), ts, self._day(ts),
                    actor_id=env.actor_id, actor_name=env.actor_name,
                )
                message_ids.append(message_id)
                self.life.record_message(message_id)
            if assistant_text.strip():
                reply_ts = max(ts, self.clock())
                message_id = self.repo.add_message(
                    env.conversation_id, env.scope, ROLE_ASSISTANT, assistant_text.strip(), reply_ts,
                    self._day(reply_ts), actor_id=ASSISTANT_ACTOR,
                )
                message_ids.append(message_id)
                self.life.record_message(message_id)
            self.graph.register_raw([self.repo.get_message(i) for i in message_ids],
                                    source_ref=f"record_turn:{message_ids[0]}" if message_ids else None)
            self._maybe_enqueue_summary(env.conversation_id, env.scope)
            if state_update and message_ids and user_text.strip() and not is_system_event(user_text):
                self._enqueue_state(env, message_ids[0])
        return {"conversation_id": env.conversation_id, "scope": env.scope, "message_ids": message_ids}

    def record_message(self, envelope: Dict[str, Any], role: str, content: str,
                       source_ref: Optional[str] = None, recalled: bool = False,
                       state_update: bool = True, turn_ref: Optional[str] = None) -> Dict[str, Any]:
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
                    "SELECT id,episode_id,source_ref,day_key FROM messages WHERE source_ref=?", (source_ref,)
                ).fetchone()
                if old:
                    self._cancel_state_for_message(old["id"])
                    # 自动状态任务用 memory 消息 id，网关手动更新用 source_ref；两者都要撤回。
                    self.repo.temporal.retract_source(env.scope, f"message:{old['id']}")
                    if old["source_ref"]:
                        self.repo.temporal.retract_source(env.scope, old["source_ref"])
                    self.repo.conn.execute(
                        "UPDATE jobs SET status='done',dedupe_key=NULL WHERE kind=? AND conversation_id=? "
                        "AND status IN ('pending','running')",
                        (JOB_SUMMARIZE, env.conversation_id),
                    )
                    self._retract_message_memories(env.scope, dict(old))
                    self.repo.conn.execute(
                        "UPDATE messages SET content='[消息已撤回]',origin='recalled' WHERE id=?", (old["id"],)
                    )
                    node = self.graph.for_message(old["id"])
                    if node:
                        self.graph.invalidate(node["id"])
            message_id = self.repo.add_message(
                env.conversation_id, env.scope, role, content.strip(), ts, self._day(ts),
                actor_id=actor_id, actor_name="" if role == ROLE_ASSISTANT else env.actor_name,
                source_ref=source_ref,
                origin="recalled" if recalled else ORIGIN_LIVE,
            )
            self.provenance.register_message(env.scope, source_ref, message_id, turn_ref=turn_ref)
            self.life.record_message(message_id)
            if not recalled:
                previous = self.graph.for_message(message_id)
                if turn_ref:
                    row = self.repo.conn.execute(
                        "SELECT id FROM association_nodes WHERE scope=? AND source_ref=? AND source_type='raw'",
                        (env.scope, turn_ref)).fetchone()
                    previous = self.graph.get(row[0]) if row else previous
                    ids = list(dict.fromkeys([*(previous["message_ids"] if previous else []), message_id]))
                    node = self.graph.register_raw([self.repo.get_message(i) for i in ids], source_ref=turn_ref)
                    completed = self.repo.conn.execute(
                        "SELECT record FROM experiences WHERE source_ref=? AND scope=?", (turn_ref, env.scope)).fetchone()
                    done = completed and json.loads(completed[0]).get("status") != "started"
                    if node and not done:
                        self.repo.conn.execute("UPDATE association_nodes SET waiting_until=? WHERE id=?",
                                               (self.clock() + 2 * self.settings.job_lease_seconds, node["id"]))
                elif not previous:
                    self.graph.register_raw([self.repo.get_message(message_id)])
            self._maybe_enqueue_summary(env.conversation_id, env.scope)
            if state_update and role == ROLE_USER and not recalled and not is_system_event(content):
                self._enqueue_state(env, message_id)
        return {"conversation_id": env.conversation_id, "scope": env.scope, "message_ids": [message_id]}

    def _retract_message_memories(self, scope: str, message: Dict[str, Any]) -> None:
        invalid = self.dependencies.descendants(scope, [("message", message["id"])])
        if message["episode_id"]:
            invalid.add(message["episode_id"])
        days = {message["day_key"]}
        while True:
            linked = self.dependencies.descendants(scope, [("memory", mid) for mid in invalid])
            for day in days:
                linked.update(r[0] for r in self.repo.conn.execute(
                    "SELECT id FROM memories WHERE scope=? AND kind='diary' AND day_key=? AND deleted=0", (scope, day)))
            expanded = invalid | linked
            days.update(m["day_key"] for mid in expanded if (m := self.repo.get_memory(mid)) and m["day_key"])
            if expanded == invalid:
                break
            invalid = expanded
        for memory_id in invalid:
            self.delete_memory(memory_id)
            self.repo.conn.execute("UPDATE messages SET episode_id=NULL WHERE episode_id=?", (memory_id,))
        for day in days:
            self.diary_queue.mark(scope, day)
            self.repo.conn.execute(
                "UPDATE jobs SET status='done',lease_until=NULL,dedupe_key=NULL,updated_at=? "
                "WHERE kind=? AND scope=? AND status IN ('pending','running') "
                "AND json_extract(payload, '$.day_key')=?",
                (self.clock(), JOB_DIARY, scope, day),
            )

    # ---------- context ----------
    def build_context(self, envelope: Dict[str, Any], query: str = "") -> Dict[str, Any]:
        env = self.resolve(envelope)
        scopes = scope_policy.recall_scopes(env.scope)
        working = self.repo.recent_messages(env.conversation_id, self.settings.working_window)
        window_start = working[0]["ts"] if working else None
        bridge = self.repo.latest_episode_before(env.conversation_id, window_start)
        if bridge and self.retention.direct(bridge, "memory") is False:
            bridge = None
        if bridge:
            node = self.graph.for_memory(bridge["id"]) or self.graph.register_memory(bridge["id"])
            if self.graph.valid(node):
                bridge = {**bridge, "node_id": node["id"], "citation_id": node["id"],
                          "node_digest": node["digest"], "source_family": node["family"],
                          "corrections": self.corrections.inherited_facts(node)}
            else:
                bridge = None

        if bridge:
            bridge["temporal"] = self.repo.temporal.describe_memory_ids([bridge["id"]]).get(bridge["id"])

        recall_query = self._recall_query(query or env.content, working)
        excluded = {bridge["id"]} if bridge else set()
        recalled = self._recall(
            recall_query, scopes, [KIND_EPISODE, KIND_EXCERPT, KIND_DIARY, "message"], excluded,
            env.conversation_id, window_start, include_working=True,
        )
        recall_trace = copy.deepcopy(getattr(self.retriever, "last_trace", {}))
        recall_backfill = [item for item in recalled if item.pop("overlaps_working", False)]
        backfill_ids = {item["id"] for item in recall_backfill}
        recalled = [item for item in recalled if item["id"] not in backfill_ids]
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
        profile = self._profile(env, scopes, recall_query)
        profile_trace = copy.deepcopy(getattr(self.retriever, "last_trace", {}))
        return {
            "envelope": env.to_dict(),
            "scope": env.scope,
            "conversation_id": env.conversation_id,
            "recall_query": recall_query,
            "working": [_message_view(m) for m in working],
            "other_channels": other_channels,
            "bridge": _memory_view(bridge) if bridge else None,
            "recalled": episodes + diaries,
            "recall_backfill": recall_backfill,
            "profile": profile,
            "retrieval_trace": {"recall": recall_trace, "profile": profile_trace},
            "state": visible_state,
            "life": self.life.snapshot(env.scope),
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
        include_working: bool = False,
    ) -> List[Dict[str, Any]]:
        rows = self.retriever.search(query, scopes, kinds=kinds,
                                     k=self.settings.retrieval_candidates, excluded=excluded)
        visible = []
        for row in rows:
            if row["kind"] == KIND_EXCERPT and (row.get("meta") or {}).get("curated_quote") != 1:
                continue
            overlaps_window = (
                row["kind"] == KIND_EPISODE
                and row.get("origin") != "experience"
                and row.get("conversation_id") == conversation_id
                and window_start is not None
                and (row.get("end_ts") or 0) >= window_start
            )
            if row["kind"] == "message" and window_start is not None:
                message = self.repo.get_message(row["message_id"])
                overlaps_window = bool(message and message["conversation_id"] == conversation_id and message["ts"] >= window_start)
            if overlaps_window and not include_working:
                continue
            visible.append(_memory_view(row, **({"overlaps_working": True} if overlaps_window else {})))
        return visible

    def _profile(self, env: ConversationEnvelope, scopes: Sequence[str], query: str) -> List[Dict[str, Any]]:
        subject = OWNER_ACTOR if env.scope == OWNER_SCOPE else env.actor_id
        facts = self.repo.list_memories(
            kinds=[KIND_SEMANTIC], scopes=scopes, subject_id=subject, order="profile",
            limit=max(32, self.settings.profile_k),
        )
        _, historical = time_intent(query)
        current_ids = set(self.repo.temporal.visible_memory_ids([f["id"] for f in facts], include_history=historical))
        facts = [f for f in facts if f["id"] in current_ids]
        relevant = self._recall(query, scopes, [KIND_SEMANTIC], (), env.conversation_id, None)
        # Reserve a bounded identity slot inside the configured budget.
        selected = [f for f in facts if f.get("origin") == "legacy"][:min(1, self.settings.profile_k)]
        selected += [f for f in relevant if f.get("subject_id") == subject
                     and f["id"] not in {item["id"] for item in selected}]
        selected_ids = {item["id"] for item in selected}
        selected += [f for f in facts if f["id"] not in selected_ids]
        output = []
        selected = selected[:self.settings.profile_k]
        times = self.repo.temporal.describe_memory_ids([f["id"] for f in selected])
        for fact in selected:
            node = self.graph.for_memory(fact["id"]) or self.graph.register_memory(fact["id"])
            if node and self.graph.valid(node):
                output.append(_memory_view(fact, node_id=node["id"], citation_id=node["id"],
                                           node_digest=node["digest"], source_family=node["family"],
                                           temporal=times.get(fact["id"])))
        return output

    # ---------- state ----------
    def get_state(self, scope: str = OWNER_SCOPE) -> Dict[str, Any]:
        return self.repo.get_state(scope)

    def put_state(self, scope: str, data: Dict[str, Any], expected_version: Optional[int] = None,
                  source_ref: Optional[str] = None) -> Dict[str, Any]:
        with self.repo.transaction():
            version = self.state_effects.manual(scope, data, expected_version, source_ref=source_ref)
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
        """Actual human activity is independent of the recent assistant-message tail."""
        messages = self.repo.recent_messages("local", limit)
        rows = self.repo.conn.execute(
            "SELECT * FROM messages WHERE role='user' "
            "AND (actor_id=? OR actor_id IN (SELECT 'qq:'||external_id FROM identity_links "
            "WHERE channel='qq' AND identity_id=?)) "
            "AND LOWER(origin)='live' ORDER BY ts DESC,id DESC", (OWNER_ACTOR, OWNER_ACTOR),
        )
        last_user = last_owner = None
        for row in rows:
            message = dict(row)
            if is_system_event(message["content"]):
                continue
            if last_owner is None:
                last_owner = {**_message_view(message), "conversation_id": message["conversation_id"]}
            if message["conversation_id"] == "local":
                last_user = _message_view(message)
                break
        return {
            "messages": [_message_view(m) for m in messages],
            "last_user": last_user, "last_owner_activity": last_owner,
            "state": self.repo.get_state(OWNER_SCOPE)["data"],
            "life": self.life.snapshot(OWNER_SCOPE),
        }

    def life_snapshot(self, scope: str = OWNER_SCOPE) -> Dict[str, Any]:
        return self.life.snapshot(scope)

    def regulate_life(self, scope: str, mode: str, duration_minutes: int = 0,
                      reason: str = "", source_ref: str = "") -> Dict[str, Any]:
        with self.repo.transaction():
            return self.life.regulate(
                scope, mode, duration_minutes, reason, source_ref, now=self.clock()
            )

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
        self._enqueue_summary_chunk(conversation_id, scope, chunk)

    def _enqueue_summary_chunk(self, conversation_id: str, scope: str, chunk: Sequence[Dict[str, Any]]) -> bool:
        # A trusted raw turn is indivisible, including interleaved gateway messages.
        expanded = {m["id"]: m for m in chunk}
        for message in chunk:
            node = self.graph.for_message(message["id"])
            for mid in node["message_ids"] if node else ():
                source = self.repo.get_message(mid)
                if source and source["episode_id"] is None and source["day_key"] == chunk[0]["day_key"]:
                    expanded[mid] = source
        chunk = sorted(expanded.values(), key=lambda m: (m["ts"], m["id"]))
        ids = [m["id"] for m in chunk]
        payload = {"from_id": min(ids), "to_id": max(ids), "day_key": chunk[0]["day_key"],
                   "source_message_ids": ids}
        digest = content_hash(json.dumps(ids))
        return self.repo.enqueue_job(
            JOB_SUMMARIZE, scope, payload,
            dedupe_key=f"{JOB_SUMMARIZE}:{conversation_id}:{digest}", conversation_id=conversation_id,
        ) is not None

    def enqueue_due_summaries(self) -> int:
        """Flush small conversations after idle time instead of waiting for 40 messages."""
        before = self.clock() - self.settings.summarize_idle_seconds
        created = 0
        for row in self.repo.idle_pending_conversations(
            before, self.settings.summarize_idle_min, self._day(self.clock()),
        ):
            conversation_id, scope = row["conversation_id"], row["scope"]
            if self.repo.has_open_job(JOB_SUMMARIZE, conversation_id):
                continue
            chunk = self.repo.oldest_pending_messages(conversation_id, self.settings.summarize_chunk)
            if not chunk:
                continue
            created += int(self._enqueue_summary_chunk(conversation_id, scope, chunk))
        return created

    def _diary_episodes(self, scope: str, day: str) -> List[Dict[str, Any]]:
        if self._past_day_bounds(day) is None or self.repo.has_pending_messages_for_day(scope, day):
            return []
        episodes = [
            e for e in self.repo.episodes_for_day(scope, day)
            if e["content"].strip() and not is_system_event(e["content"])
            and not e["meta"].get("audit_only") and e["meta"].get("status") != "started"
            and not e["meta"].get("identity_rebuild_pending")
            and e["origin"] not in ("recalled", "withdrawn", "retracted", "deleted", "system", "archived")
        ]
        if any(e["meta"].get("repair_pending") or e["meta"].get("experience_summary_pending") for e in episodes):
            return []
        return episodes

    @staticmethod
    def _episode_signature(episodes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        material = [
            [e["id"], e["content"], e["meta"].get("source_message_ids", []),
             e["meta"].get("source_memory_ids", [])]
            for e in episodes
        ]
        digest = hashlib.sha256(json.dumps(
            material, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {"episode_ids": [e["id"] for e in episodes], "episodes_hash": digest}

    def _diary_is_current(self, scope: str, day: str, signature: Dict[str, Any]) -> bool:
        diaries = self.repo.list_memories(kinds=[KIND_DIARY], scopes=[scope], day_key=day, limit=2)
        if len(diaries) != 1:
            return False
        meta = diaries[0]["meta"]
        if "episodes_hash" not in meta and meta.get("episode_id"):
            return signature["episode_ids"] == [meta["episode_id"]]
        return all(meta.get(key) == value for key, value in signature.items())

    def enqueue_due_diaries(self) -> int:
        today = self._day(self.clock())
        created = 0
        with self.repo.transaction():
            self.diary_queue.backfill(getattr(self.settings, "diary_backfill_batch", 256))
            for dirty in self.diary_queue.due(today, getattr(self.settings, "diary_maintenance_batch", 32)):
                scope, day = dirty["scope"], dirty["day_key"]
                if self._past_day_bounds(day) is None:
                    self.diary_queue.ack(dirty)
                    continue
                episodes = self._diary_episodes(scope, day)
                if not episodes:
                    if self.repo.has_pending_messages_for_day(scope, day) or self.repo.episodes_for_day(scope, day):
                        self.diary_queue.defer(dirty)
                    else:
                        # Only generated diaries lose their source when the last
                        # episode is removed; preserve unsourced/manual entries.
                        for row in self.repo.conn.execute("SELECT id,meta FROM memories WHERE scope=? AND kind='diary' "
                                                          "AND day_key=? AND deleted=0", (scope, day)).fetchall():
                            meta = json.loads(row["meta"])
                            if meta.get("episode_ids") or meta.get("episode_id"):
                                self.delete_memory(row["id"])
                        self.diary_queue.ack(dirty)
                    continue
                signature = self._episode_signature(episodes)
                if self._diary_is_current(scope, day, signature):
                    self.diary_queue.ack(dirty)
                    continue
                if self.repo.has_open_diary_job(scope, day):
                    self.diary_queue.defer(dirty)
                    continue
                if self.repo.enqueue_job(
                    JOB_DIARY, scope, {"day_key": day, **signature},
                    dedupe_key=f"{JOB_DIARY}:{scope}:{day}:{signature['episodes_hash']}",
                ):
                    created += 1
                self.diary_queue.defer(dirty)
        return created

    def claim_job(self, kinds: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
        now = self.clock()
        self.maintenance.tick()
        self.dependencies.sync(limit=256)
        self.provenance.reconcile(limit=100)
        if now - self._last_diary_check >= DIARY_CHECK_INTERVAL:
            self._last_diary_check = now
            self.enqueue_due_summaries()
            self.enqueue_due_diaries()
            self.reconcile_index(full=False)
            self._last_index_check = now
        elif now - self._last_index_check >= INDEX_CHECK_INTERVAL:
            # Keep a backlog moving without coupling every batch to the much
            # slower diary, graph-backfill and retention maintenance interval.
            self._reconcile_vectors(full=False)
            self._last_index_check = now
        deferred = []
        while True:
            job = self.repo.claim_job(self.settings.job_lease_seconds,
                                     kinds=kinds or [JOB_ASSOCIATE, JOB_SUMMARIZE, JOB_DIARY, "summarize_experience", "repair_memory", "audit_memory"],
                                     excluded=deferred, max_lease_recoveries=self.settings.job_max_lease_recoveries)
            if job is None:
                return None
            with self.repo.transaction():
                materials = self._job_materials(job)
            if materials is False:
                self.repo.conn.execute("UPDATE jobs SET status='pending',lease_until=NULL WHERE id=?", (job["id"],))
                deferred.append(job["id"])
                continue
            if materials is not None:
                return {"job": job, "materials": materials}
            self.repo.finish_job(job["id"], release_dedupe=job["kind"] in {JOB_DIARY, JOB_SUMMARIZE})
            if job["kind"] == JOB_SUMMARIZE:
                self._maybe_enqueue_summary(job["conversation_id"], job["scope"])
                self.enqueue_due_summaries()

    def _summary_messages(self, job: Dict[str, Any]) -> List[Dict[str, Any]]:
        payload = job["payload"]
        if "source_message_ids" in payload:
            candidates = [self.repo.get_message(int(i)) for i in payload["source_message_ids"]]
        else:
            candidates = self.repo.pending_messages_in_range(
                job["conversation_id"], payload["from_id"], payload["to_id"],
            )
        snapshot = self.retention.get(job["id"])
        if not snapshot:
            expanded = {m["id"]: m for m in candidates if m}
            for message in list(expanded.values()):
                node = self.graph.for_message(message["id"])
                for mid in node["message_ids"] if node else ():
                    source = self.repo.get_message(mid)
                    if source:
                        expanded[mid] = source
            candidates = list(expanded.values())
        messages = sorted(
            (m for m in candidates if m and m["conversation_id"] == job["conversation_id"]
             and m["scope"] == job["scope"] and m["episode_id"] is None
             and consolidation_candidate(m["role"], m["content"], m["origin"])),
            key=lambda m: (m["ts"], m["id"]),
        )
        day = payload.get("day_key") or (messages[0]["day_key"] if messages else None)
        return [m for m in messages if m["day_key"] == day]

    def _diary_state(self, scope: str, day: str) -> Dict[str, Any]:
        bounds = self._past_day_bounds(day)
        if bounds is None:
            return {}
        start, end = bounds
        history = self.repo.temporal.state_history(scope, as_of=math.nextafter(end, -math.inf), limit=1)
        if not history or history[0]["effective_at"] < start:
            return {}
        snapshot = history[0]
        # Full snapshots inherit earlier versions; a backdated version may contain future state.
        unsafe = self.repo.conn.execute(
            "SELECT 1 FROM state_history WHERE scope = ? AND version <= ? "
            "AND (redacted = 1 OR effective_at > ? OR recorded_at > ?) LIMIT 1",
            (scope, snapshot["version"], snapshot["effective_at"], end),
        ).fetchone()
        return {} if unsafe else snapshot["data"]

    def _job_materials(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if job["kind"] == "audit_memory":
            return self.turn_audit.materials(job)
        if job["kind"] == JOB_ASSOCIATE:
            node = self.graph.get(job["payload"]["node_id"], job["scope"])
            if not self.graph.valid(node) or node["revision"] != job["payload"]["revision"]:
                return None
            if node["waiting_until"] > self.clock():
                return False
            material = self.graph.material(node)
            if node["source_type"] == "raw":
                human = [m["content"] for m in material["sources"] if m["role"] != ROLE_ASSISTANT]
                query = "\n".join(human or [m["content"] for m in material["sources"]])
                excluded = [f"message:{mid}" for mid in node["message_ids"]]
            else:
                query, excluded = material["sources"][0]["content"], [node["memory_id"]]
            payload = job["payload"]
            if "candidate_versions" not in payload:
                hits = self.retriever.search(query[:4000], [job["scope"]],
                                             k=self.settings.association_candidates, excluded=excluded,
                                             expand_graph=False, include_evidence=False)
                candidates, families = [], set()
                for hit in hits:
                    candidate = (self.graph.for_memory(hit["memory_id"]) if hit["memory_id"]
                                 else self.graph.for_message(hit["message_id"]))
                    independent = candidate and self.graph.independent(candidate, node)
                    if independent and candidate["id"] != node["id"] and self.graph.valid(candidate):
                        family = self.graph.evidence_family(candidate)
                        if families.intersection(family):
                            continue
                        families.update(family)
                        candidates.append(candidate)
                candidates = list({c["id"]: c for c in candidates}.values())[:self.settings.association_candidates]
                payload = {**payload, "candidate_versions": {c["id"]: c["digest"] for c in candidates},
                           "correction_waterline": self.corrections.waterline(job["scope"])}
                self.repo.update_job_payload(job["id"], payload)
                job["payload"] = payload
            else:
                candidates = [self.graph.get(nid, job["scope"]) for nid in payload["candidate_versions"]]
                if (payload.get("correction_waterline", 0) != self.corrections.waterline(job["scope"])
                        or any(not self.graph.valid(c) or c["digest"] != payload["candidate_versions"][c["id"]]
                               for c in candidates)):
                    # Recreate bounded materials after a source revision; no old model result is accepted.
                    self.repo.update_job_payload(job["id"], {k: v for k, v in payload.items()
                                                            if k != "candidate_versions"})
                    job["payload"].pop("candidate_versions")
                    return self._job_materials(job)
            def view(candidate):
                return {"id": candidate["id"], "revision": candidate["revision"],
                        "content": _association_excerpt(self.graph.material(candidate)["text"], query[:4000]),
                        "corrections": self.corrections.facts(candidate["id"], job["scope"]),
                        "source_type": candidate["source_type"]}
            chains = self.association_planner.relevant_chains(job["scope"], query)
            for chain in chains:
                constraint = self.corrections.reason(job["scope"], chain["id"])
                if constraint:
                    chain["correction"] = {k: constraint[k] for k in ("kind", "replacement", "explanation")}
            plan = self.association_planner.prepare(
                job, node, candidates, query, chains)
            return {"scope": job["scope"], "source": view(node),
                    "candidates": [view(c) for c in candidates], "chains": chains,
                    "compact": plan["prompt"]}
        if job["kind"] == "repair_memory":
            return self.corrections.repair_materials(job)
        if job["kind"] == "summarize_experience":
            row = self.repo.conn.execute("SELECT * FROM experiences WHERE source_ref=?",
                                        (job["payload"]["source_ref"],)).fetchone()
            if not row or row["revision"] != job["payload"]["revision"]:
                return None
            memory = self.repo.get_memory(row["memory_id"])
            if not memory or memory["deleted"]:
                return None
            node = self.graph.for_memory(memory["id"])
            payload = {**job["payload"], "target_digest": digest([memory["content"], memory["meta"]])}
            self.repo.update_job_payload(job["id"], payload)
            job["payload"] = payload
            record = json.loads(row["record"])
            record["fact_corrections"] = self.corrections.inherited_facts(node) if node else []
            from .experience import text
            raw = text(record.get("result", ""))
            # Bound model context; the independently indexed full evidence remains intact.
            record["result"] = raw if len(raw) <= 18000 else raw[:12000] + "\n[中段省略]\n" + raw[-6000:]
            return {"scope": row["scope"], "record": record}
        if job["kind"] == JOB_STATE:
            message = self.repo.get_message(int(job["payload"]["message_id"]))
            if not message or not consolidation_candidate(message["role"], message["content"], message["origin"]):
                return None
            state = self.repo.get_state(job["scope"])
            # 后台判断逐条使用真实消息；不等待 40 条摘要，也不把其他群成员混入个人状态。
            return {
                "scope": job["scope"], "messages": [_message_view(message)],
                "state": state["data"], "version": state["version"],
            }
        if job["kind"] == JOB_SUMMARIZE:
            messages = self._summary_messages(job)
            if not messages:
                return None
            payload = {**job["payload"], "day_key": messages[0]["day_key"],
                       "source_message_ids": [m["id"] for m in messages]}
            self.repo.update_job_payload(job["id"], payload)
            job["payload"] = payload
            nodes_by_id = {}
            for message in messages:
                node = self.graph.for_message(message["id"])
                if not self.graph.valid(node):
                    sources = [self.repo.get_message(i) for i in node["message_ids"]] if node else [message]
                    sources = [m for m in sources if m and m["scope"] == job["scope"]
                               and consolidation_candidate(m["role"], m["content"], m["origin"])]
                    source_ref = node["source_ref"] if node and len(sources) == len(node["message_ids"]) else None
                    node = self.graph.register_raw(sources, source_ref)
                if node:
                    nodes_by_id[node["id"]] = node
            nodes = list(nodes_by_id.values())
            if any(n["waiting_until"] > self.clock() for n in nodes):
                return False
            snapshot = self.retention.freeze(job, messages, nodes, self.settings.raw_protect_k)
            if snapshot is False:
                return False
            if snapshot is None:
                return None
            return {
                "scope": job["scope"],
                "conversation_id": job["conversation_id"],
                "day_key": payload["day_key"],
                "source_message_ids": payload["source_message_ids"],
                "messages": [_message_view(m) for m in messages],
                "corrections": [fact for n in nodes for fact in self.corrections.facts(n["id"], job["scope"])],
                "retention": snapshot,
                "fact_keys": [
                    {"subject": f["subject"], "predicate": f["predicate"]}
                    for f in self.repo.temporal.facts(job["scope"], limit=50)
                ],
            }
        if job["kind"] == JOB_DIARY:
            day = job["payload"]["day_key"]
            episodes = self._diary_episodes(job["scope"], day)
            if not episodes:
                return None
            signature = self._episode_signature(episodes)
            if self._diary_is_current(job["scope"], day, signature):
                return None
            payload = {**job["payload"], **signature}
            self.repo.update_job_payload(job["id"], payload)
            job["payload"] = payload
            nodes = [self.graph.register_memory(e["id"]) for e in episodes]
            nodes = [n for n in nodes if n]
            snapshot = self.retention.freeze(job, episodes, nodes, self.settings.episode_protect_k)
            if snapshot is False:
                return False
            if snapshot is None:
                return None
            return {
                "scope": job["scope"],
                "day_key": day,
                "episodes": [e["content"] for e in episodes],
                **signature,
                "state": self._diary_state(job["scope"], day),
                "life": self.life.day_summary(day, job["scope"]),
                "retention": snapshot,
            }
        return None

    def complete_association(self, job_id: int, links: List[Dict[str, Any]], prior: float = .5,
                             expected_attempt=None, rejected_links=0, decisions=None) -> Dict[str, Any]:
        with self.repo.transaction():
            job = self._running_job(job_id, JOB_ASSOCIATE, expected_attempt)
            if not job:
                return {"status": "ignored"}
            if type(rejected_links) is not int or not 0 <= rejected_links <= 40:
                raise ValueError("丢弃的关联条目数量无效")
            resolved = None
            if decisions is not None:
                links, resolved = self.association_planner.decode(job, decisions)
            current_plan = self.association_planner.current(job)
            unresolved = [job["payload"]["association_plan"]["aliases"][alias]
                          for alias, value in (resolved or {}).items() if value is None]
            if current_plan and unresolved and not job["payload"].get("evidence_expanded"):
                # One immediate evidence expansion, separate from transport failures.
                # A genuine ambiguity must never enter the outage recovery loop.
                self.repo.update_job_payload(job_id, {**job["payload"], "evidence_expanded": True})
                self.repo.conn.execute("UPDATE jobs SET status='pending',lease_until=NULL WHERE id=?", (job_id,))
                return {"status": "expanding_evidence"}
            if current_plan and self.graph.submit(job, links, prior):
                if resolved is not None:
                    self.association_planner.remember(job, resolved)
                more = self.association_planner.advance(job, resolved)
                unresolved = job["payload"].get("association_unresolved", unresolved)
                self.repo.update_job_payload(job_id, {**job["payload"], "rejected_links": rejected_links,
                                                     "unresolved_targets": unresolved})
                if more:
                    self.repo.conn.execute("UPDATE jobs SET status='pending',lease_until=NULL WHERE id=?", (job_id,))
                    return {"status": "continuing_association", "node_id": job["payload"]["node_id"]}
                self.repo.finish_job(job_id)
                return {"status": "done", "node_id": job["payload"]["node_id"],
                        "rejected_links": rejected_links, "unresolved_targets": unresolved}
            # Invalidate only model materials, retaining the same source job when still current.
            node = self.graph.get(job["payload"]["node_id"], job["scope"])
            if self.graph.valid(node) and node["revision"] == job["payload"]["revision"]:
                self.repo.update_job_payload(job_id, {k: v for k, v in job["payload"].items()
                                                     if k != "candidate_versions"})
                self.repo.conn.execute("UPDATE jobs SET status='pending',lease_until=NULL WHERE id=?", (job_id,))
            else:
                self.repo.finish_job(job_id)
            return {"status": "stale"}

    def complete_state_update(self, job_id: int, changes: Dict[str, Any],
                              expected_version: Optional[int] = None, expected_attempt=None) -> Dict[str, Any]:
        """按消息去重，状态增量和任务完成在同一事务中提交，重试不重复加好感度。"""
        from tools.memory_tool import _apply_changes
        import inspect

        with self.repo.transaction():
            job = self._running_job(job_id, JOB_STATE, expected_attempt)
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
            message = self.repo.get_message(job["payload"]["message_id"])
            changed = self.state_effects.automatic(job["scope"], message, changes, expected_version)
            self.repo.finish_job(job["id"])
        return {"status": "done", "scope": job["scope"], "changed": changed}

    def _running_job(self, job_id: int, kind: str, expected_attempt=None) -> Optional[Dict[str, Any]]:
        job = self.repo.get_job(int(job_id))
        if not job or job["kind"] != kind or job["status"] != "running":
            return None
        if expected_attempt is not None and (type(expected_attempt) is not int
                                             or job["attempts"] != expected_attempt
                                             or job["lease_until"] <= self.clock()):
            return None
        return job

    def complete_summary(
        self,
        job_id: int,
        summary: str,
        tags: Sequence[str] = (),
        facts: Sequence[Dict[str, Any]] = (),
        importance: float = 5.0,
        retrieval: Optional[Dict[str, Any]] = None,
        expected_attempt=None,
    ) -> Dict[str, Any]:
        job = self._running_job(job_id, JOB_SUMMARIZE, expected_attempt)
        if job is None:
            return {"status": "ignored"}
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("summary is empty")
        conversation_id, scope = job["conversation_id"], job["scope"]
        new_ids: List[str] = []
        with self.repo.transaction():
            messages = self._summary_messages(job)
            if not messages:
                self.repo.finish_job(job["id"])
                return {"status": "done", "episode_id": None, "scope": scope}
            if not self.retention.valid(job_id, messages):
                self.repo.finish_job(job_id, release_dedupe=True)
                self._enqueue_summary_chunk(conversation_id, scope, messages)
                return {"status": "stale", "episode_id": None, "scope": scope}
            from .corrections import validate_corrected_text
            nodes = {n["id"]: n for m in messages if (n := self.graph.for_message(m["id"]))}
            constraints = [fact for n in nodes.values() for fact in self.corrections.facts(n["id"], scope)]
            validate_corrected_text(summary, constraints)
            for fact in facts or ():
                validate_corrected_text(str(fact.get("content") or ""), constraints)
            valid_facts = []
            human_messages = [m for m in messages if m["role"] == ROLE_USER and m["origin"] == ORIGIN_LIVE]
            actors = {m["actor_id"] for m in human_messages if m["actor_id"]}
            for fact in facts or ():
                # Personal facts must be attributed to the speaker of the cited
                # message. Preserve old unversioned callers only when exactly one
                # participant makes the attribution unambiguous.
                subject = str(fact.get("subject") or "").strip()
                if scope == OWNER_SCOPE and subject in ("", "user"):
                    subject = OWNER_ACTOR
                elif not subject and len(actors) == 1:
                    subject = next(iter(actors))
                source_id = fact.get("source_message_id")
                source = next((m for m in human_messages if str(m["id"]) == str(source_id)
                               and m["actor_id"] == subject), None)
                legacy = source_id is None and not fact.get("predicate") and actors == {subject}
                if source or legacy:
                    valid_facts.append({**fact, "subject": subject})
            fresh_facts = self._fresh_facts(scope, valid_facts)
            message_ids = [m["id"] for m in messages]
            day = messages[0]["day_key"]
            end_ts = max(m["ts"] for m in messages)
            episode_id = self.repo.add_memory(
                KIND_EPISODE, scope, summary, conversation_id=conversation_id,
                importance=self._clamp_importance(importance), start_ts=min(m["ts"] for m in messages),
                end_ts=end_ts, day_key=day, tags=[str(t) for t in tags][:8],
                meta={"source_message_ids": message_ids,
                      "source_memory_ids": job["payload"].get("source_memory_ids", []),
                      "retrieval": retrieval or {}, "facts": [f["content"] for f in fresh_facts]},
            )
            new_ids.append(episode_id)
            self.repo.mark_consolidated_ids(conversation_id, message_ids, episode_id)
            protected = self.retention.apply(job_id, episode_id)
            for fact in fresh_facts:
                subject = str(fact.get("subject") or "").strip()
                if scope == OWNER_SCOPE and subject in ("", "user"):
                    subject = OWNER_ACTOR
                source_id = fact.get("source_message_id")
                try:
                    source_id = int(source_id) if source_id is not None else None
                except (TypeError, ValueError):
                    source_id = None
                source_message = next((m for m in messages if m["id"] == source_id
                                       and m["actor_id"] == subject and m["role"] != ROLE_ASSISTANT), None)
                fact_id = self.repo.add_memory(
                    KIND_SEMANTIC, scope, fact["content"], conversation_id=conversation_id,
                    subject_id=subject if subject in actors else None,
                    importance=self._clamp_importance(fact.get("importance", 6)), end_ts=end_ts,
                    day_key=day,
                    meta={"episode_id": episode_id,
                          "source_message_ids": [source_id] if source_message else message_ids,
                          "fact_source_message_id": source_id if source_message else None,
                          "retrieval": retrieval or {}},
                )
                new_ids.append(fact_id)
                if fact.get("predicate") and "value" in fact and source_message:
                    valid_from = fact.get("valid_from")
                    try:
                        valid_from = float(valid_from) if valid_from is not None else source_message["ts"]
                    except (TypeError, ValueError):
                        valid_from = source_message["ts"]
                    self.repo.temporal.put_fact(fact_id, scope, subject, fact["predicate"], fact["value"],
                                                float(valid_from), source_ref=episode_id)
            for memory_id in new_ids:
                self.graph.register_memory(memory_id)
            self.repo.finish_job(job["id"])
            self._maybe_enqueue_summary(conversation_id, scope)
            self.enqueue_due_summaries()
            self.enqueue_due_diaries()
        self._index_ids(new_ids)
        return {"status": "done", "episode_id": episode_id, "semantic_ids": new_ids[1:], "scope": scope,
                "protected_raw": protected}

    def _fresh_facts(self, scope: str, facts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fresh = []
        for fact in facts or ():
            text = str((fact or {}).get("content") or "").strip()
            if not text:
                continue
            if fact.get("predicate") and "value" in fact:
                # 同一句偏好可能在 A→B→A 后再次成立，不按全文相似度误删新版本。
                fresh.append({**fact, "content": text})
                continue
            existing = self.repo.find_memory_by_hash(scope, KIND_SEMANTIC, content_hash(text), fact.get("subject"))
            if existing is None:
                try:
                    near = self.index.query(text, [scope], [KIND_SEMANTIC], n=1, subject_id=fact.get("subject"))
                except Exception:
                    near = []
                if near and near[0][1] < SEMANTIC_DUPLICATE_DISTANCE:
                    existing = near[0][0]
            if existing:
                self.repo.touch_memory(existing)
                continue
            fresh.append({**fact, "content": text})
        return fresh

    def complete_diary(self, job_id: int, text: str, expected_attempt=None) -> Dict[str, Any]:
        job = self._running_job(job_id, JOB_DIARY, expected_attempt)
        if job is None:
            return {"status": "ignored"}
        text = (text or "").strip()
        if not text:
            raise ValueError("diary is empty")
        day = job["payload"]["day_key"]
        with self.repo.transaction():
            episodes = self._diary_episodes(job["scope"], day)
            signature = self._episode_signature(episodes)
            if (not episodes or not self.retention.valid(job_id, episodes)
                    or any(job["payload"].get(key) != value for key, value in signature.items())):
                self.repo.finish_job(job["id"], release_dedupe=True)
                self.diary_queue.mark(job["scope"], day)
                self.enqueue_due_summaries()
                self.enqueue_due_diaries()
                return {"status": "ignored", "diary_id": None}
            if self._diary_is_current(job["scope"], day, signature):
                self.repo.finish_job(job["id"], release_dedupe=True)
                return {"status": "done", "diary_id": None}
            diaries = self.repo.list_memories(kinds=[KIND_DIARY], scopes=[job["scope"]], day_key=day, limit=100000)
            meta = {**(diaries[0]["meta"] if diaries else {}), **signature}
            for key in ("source_message_ids", "source_memory_ids"):
                meta[key] = list(dict.fromkeys(
                    source for e in episodes for source in (e["meta"].get(key) or [])
                ))
            if diaries:
                diary_id = diaries[0]["id"]
                self.repo.update_memory(diary_id, content=text, meta=meta, importance=8)
                for duplicate in diaries[1:]:
                    self.delete_memory(duplicate["id"])
            else:
                diary_id = self.repo.add_memory(KIND_DIARY, job["scope"], text, importance=8, day_key=day, meta=meta)
            protected = self.retention.apply(job_id, diary_id)
            self.graph.register_memory(diary_id)
            self.repo.finish_job(job["id"], release_dedupe=True)
        if diary_id:
            self._index_ids([diary_id])
        return {"status": "done", "diary_id": diary_id, "protected_episodes": protected}

    def fail_job(self, job_id: int, error: str, expected_attempt=None) -> Dict[str, Any]:
        with self.repo.transaction():
            job = self.repo.get_job(int(job_id))
            if job and job["status"] != "running" and expected_attempt is None:
                return {"status": job["status"]}
            if not job or not self._running_job(job_id, job["kind"], expected_attempt):
                return {"status": "ignored"}
            status = self.repo.fail_job(int(job_id), error or "unknown error", self.settings.job_max_attempts)
            if status == "failed" and job["kind"] == "summarize_experience":
                row = self.repo.conn.execute("SELECT memory_id,revision FROM experiences WHERE source_ref=?",
                                            (job["payload"]["source_ref"],)).fetchone()
                memory = self.repo.get_memory(row["memory_id"]) if row else None
                if memory and row["revision"] == job["payload"]["revision"] and not memory["deleted"]:
                    self.repo.update_memory(memory["id"], meta={**memory["meta"],
                        "experience_summary_pending": False, "experience_summary_failed": True})
                    self.graph.register_memory(memory["id"])
            return {"status": status}

    def record_model_call(self, job_id, event, expected_attempt):
        with self.repo.transaction():
            job = self.repo.get_job(int(job_id))
            if (not job or type(expected_attempt) is not int or not 0 < expected_attempt <= job["attempts"]
                    or not isinstance(event, dict)):
                raise ValueError("模型调用缺少有效任务领取凭据")
            if event.get("status") == "started" and not self._running_job(job_id, job["kind"], expected_attempt):
                raise ValueError("模型调用所属任务租约已失效")
            result = self.model_costs.record(job, expected_attempt, event, self.settings.job_max_model_calls)
            if result["status"] == "budget_exhausted":
                self.repo.update_job_payload(job["id"], {**job["payload"], "terminal_reason": "model_call_budget"})
                self.repo.conn.execute("UPDATE jobs SET status='failed',lease_until=NULL,updated_at=?, "
                                       "last_error='model call budget exhausted' WHERE id=?", (self.clock(), job["id"]))
        if result["status"] == "budget_exhausted":
            raise ValueError("model call budget exhausted")
        return result

    @staticmethod
    def _clamp_importance(value: Any) -> float:
        try:
            return max(0.0, min(10.0, float(value)))
        except (TypeError, ValueError):
            return 5.0

    # ---------- index maintenance ----------
    def _index_ids(self, memory_ids: Sequence[str]) -> None:
        batch_size = max(1, min(512, getattr(self.settings, "index_batch_size", 64)))
        rows = list(self.repo.get_memories(list(memory_ids)[:batch_size]).values())
        self._index_rows(rows)
        self.retriever.sync(vectors=True)

    def _index_rows(self, rows: Sequence[Dict[str, Any]]) -> int:
        if not getattr(self.index, "stores_memory_vectors", True):
            return 0
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

    def reconcile_index(self, full: bool = True) -> Dict[str, int]:
        """Explicit maintenance drains the index; worker polling does one batch."""
        with self.repo.transaction():
            self.graph.backfill(limit=self.settings.association_backfill_batch)
            self.retention.enforce_budget()
        return self._reconcile_vectors(full=full)

    def _reconcile_vectors(self, full: bool = False) -> Dict[str, int]:
        keys = self.retriever.sync(vectors=True, full=full)
        if not getattr(self.index, "persistent", True) or not getattr(self.index, "stores_memory_vectors", True):
            return {"indexed": 0, "removed": 0, "keys": keys}
        indexed = 0
        batch_size = max(1, min(512, getattr(self.settings, "index_batch_size", 64)))
        while True:
            batch = self.repo.unindexed_memories(limit=batch_size)
            if not batch:
                break
            done = self._index_rows(batch)
            if done == 0:
                break
            indexed += done
            if not full:
                break
        removed = self.repo.deleted_indexed_memory_ids(limit=None if full else batch_size)
        if removed:
            self.index.delete(removed)
            self.repo.mark_indexed(removed, indexed=False)
        return {"indexed": indexed, "removed": len(removed), "keys": keys}

    def rebuild_index(self) -> Dict[str, int]:
        maintenance = getattr(self.repo, "vector_maintenance", None)
        if maintenance is not None:
            return maintenance.rebuild()
        if not getattr(self.index, "persistent", True):
            return {"indexed": 0, "removed": 0, "total": self.index.count()}
        self.index.reset()
        self.retriever.keys.clear_index_marks()
        self.repo.clear_index_marks()
        self.retriever.keys.sync(rebuild=True)
        result = self.reconcile_index(full=True)
        result["total"] = self.index.count()
        return result

    # ---------- admin ----------
    def stats(self) -> Dict[str, Any]:
        stats = self.repo.stats()
        if not getattr(self.index, "stores_memory_vectors", True):
            stats["unindexed"] = self.retriever.keys.vector_pending_count()
            stats["index_unit"] = "retrieval_keys"
        stats["index_count"] = self.index.count()
        maintenance = getattr(self.repo, "vector_maintenance", None)
        if maintenance is not None:
            stats["vector_status"] = maintenance.status()
        elif hasattr(self.index, "status"):
            stats["vector_status"] = self.index.status()
        stats["identity_links"] = self.repo.identity_links()
        stats["conversations"] = self.repo.list_conversations()
        for table in ("retrieval_keys", "state_history", "fact_versions", "association_nodes",
                      "association_chains", "association_edges", "memory_usage_events", "consolidation_snapshots"):
            try:
                stats[table] = int(self.repo.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            except Exception:
                stats[table] = 0
        stats["life_events"] = self.life.event_count()
        stats["life"] = self.life.snapshot(OWNER_SCOPE)
        stats["retention_enabled"] = self.settings.retention_enabled
        stats["associations_enabled"] = self.settings.associations_enabled
        stats["nodes_by_status"] = {r["retrieval_status"]: r["n"] for r in self.repo.conn.execute(
            "SELECT retrieval_status,COUNT(*) n FROM association_nodes WHERE superseded=0 GROUP BY retrieval_status")}
        stats["association_ready"] = self.repo.conn.execute(
            "SELECT COUNT(*) FROM association_nodes WHERE ready=1 AND superseded=0").fetchone()[0]
        stats["jobs_by_kind"] = [dict(r) for r in self.repo.conn.execute(
            "SELECT kind,status,COUNT(*) n FROM jobs GROUP BY kind,status")]
        stats["maintenance"] = self.maintenance.stats()
        stats["diary_maintenance"] = {
            "dirty_days": self.repo.conn.execute("SELECT COUNT(*) FROM diary_dirty_days").fetchone()[0],
            "backfill_cursor": self.repo.conn.execute("SELECT value FROM meta WHERE key='diary_backfill_cursor'").fetchone()[0],
        }
        stats["model_costs"] = self.model_costs.stats()
        stats["association_decisions"] = self.repo.conn.execute(
            "SELECT COUNT(*) FROM association_decisions").fetchone()[0]
        stats["unresolved_associations"] = self.repo.conn.execute(
            "SELECT COALESCE(SUM(json_array_length(payload,'$.unresolved_targets')),0) FROM jobs WHERE status='done'").fetchone()[0]
        return stats

    def list_association_nodes(self, **filters):
        from .admin import MemoryAdmin
        with self.repo.transaction():
            return MemoryAdmin(self).nodes(**filters)

    def memory_detail(self, node_id=None, memory_id=None, scope=None, message_id=None):
        from .admin import MemoryAdmin
        with self.repo.transaction():
            return MemoryAdmin(self).detail(node_id, memory_id, scope, message_id)

    def memory_graph(self, scopes=None):
        from .graph_view import graph_snapshot
        with self.repo.transaction():
            return graph_snapshot(self, scopes)

    def correct_memory(self, **params):
        with self.repo.transaction():
            result = self.corrections.apply(**params)
        self.retriever.sync()
        return result

    def complete_memory_repair(self, job_id, content, expected_attempt=None):
        with self.repo.transaction():
            job = self._running_job(job_id, "repair_memory", expected_attempt)
            if not job:
                return {"status": "ignored"}
            result = self.corrections.complete_repair(job, content)
        self.retriever.sync()
        return result

    def complete_memory_audit(self, job_id, decisions, approved, expected_attempt=None):
        with self.repo.transaction():
            job = self._running_job(job_id, "audit_memory", expected_attempt)
            if not job:
                return {"status": "ignored"}
            result = self.turn_audit.complete(job, decisions, approved)
        self.retriever.sync()
        return result

    def list_jobs(self, **filters):
        from .admin import MemoryAdmin
        return MemoryAdmin(self).jobs(**filters)

    def list_memories(self, **filters: Any) -> Dict[str, Any]:
        page_filters = {k: filters.get(k) for k in ("kinds", "scopes", "subject_id", "keyword", "day_key")}
        items = self.repo.list_memories(
            **{k: v for k, v in page_filters.items() if v},
            offset=int(filters.get("offset") or 0), limit=min(int(filters.get("limit") or 50), 500),
            order=filters.get("order") or "recent",
        )
        return {"items": items, "total": self.repo.count_memories(**{k: v for k, v in page_filters.items() if v})}

    def search(self, query: str, scopes: Sequence[str], kinds: Optional[Sequence[str]] = None, k: int = 10,
               as_of: Optional[float] = None, include_history: bool = False, retrieval_mode: str = "auto") -> List[Dict[str, Any]]:
        return self.retriever.search(query, scopes, kinds, k, as_of=as_of, include_history=include_history,
                                     retrieval_mode=retrieval_mode)

    def search_with_trace(self, query: str, scopes: Sequence[str], k: int = 8, **filters):
        hits = self.search(query, scopes, k=k, **filters)
        return {"hits": hits, "trace": copy.deepcopy(getattr(self.retriever, "last_trace", {}))}

    def read_memory_source(self, source_id: str, scope: str, offset: int = 0, limit: int = 1024):
        if not isinstance(scope, str) or not scope or not isinstance(source_id, str) or not source_id:
            raise ValueError("source page requires an explicit scope and source id")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 2048:
            raise ValueError("source page requires offset >= 0 and limit between 1 and 2048")
        node = self.graph.get(source_id, scope)
        if not node or not self.graph.valid(node):
            return None
        material = self.graph.material(node)
        text = material["text"]
        end = min(len(text), offset + limit)
        return {"citation_id": node["id"], "node_id": node["id"], "node_digest": node["digest"],
                "kind": node["source_type"], "content": text[offset:end], "offset": offset,
                "next_offset": end if end < len(text) else None, "total_chars": len(text),
                "is_excerpt": offset > 0 or end < len(text), "corrections": self.corrections.inherited_facts(node)}

    def state_history(self, scope: str = OWNER_SCOPE, as_of: Optional[float] = None, limit: int = 20):
        return self.repo.temporal.state_history(scope, as_of, max(1, min(50, int(limit))))

    def facts(self, scope: str, subject=None, predicate=None, as_of=None, limit=20):
        return self.repo.temporal.facts(scope, subject, predicate, as_of, max(1, min(50, int(limit))))

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        return self.repo.get_memory(memory_id)

    def add_memory(self, kind: str, scope: str, content: str, **fields: Any) -> Dict[str, Any]:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown kind: {kind}")
        if not content.strip():
            raise ValueError("content is empty")
        with self.repo.transaction():
            memory_id = self.repo.add_memory(
                kind, scope, content.strip(), subject_id=fields.get("subject_id") or None,
                importance=self._clamp_importance(fields.get("importance", 5)),
                day_key=fields.get("day_key") or self._day(self.clock()), tags=fields.get("tags") or (),
                end_ts=self.clock(), origin=ORIGIN_LIVE,
            )
            self.graph.register_memory(memory_id)
        self._index_ids([memory_id])
        return {"id": memory_id}

    def update_memory(self, memory_id: str, **fields: Any) -> Dict[str, Any]:
        if "importance" in fields and fields["importance"] is not None:
            fields["importance"] = self._clamp_importance(fields["importance"])
        with self.repo.transaction():
            updated = self.repo.update_memory(memory_id, **fields)
            if updated:
                self.graph.register_memory(memory_id)
        if updated:
            self._index_ids([memory_id])
        return {"updated": updated}

    def delete_memory(self, memory_id: str) -> Dict[str, Any]:
        with self.repo.transaction():
            experience = self.repo.conn.execute(
                "SELECT source_ref FROM experiences WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if experience:
                self.life.retract_experience(experience["source_ref"])
            self.repo.temporal.delete_memory(memory_id)
            deleted = self.repo.delete_memory(memory_id)
            node = self.graph.for_memory(memory_id)
            if node:
                self.graph.invalidate(node["id"])
            self.repo.conn.execute("UPDATE experiences SET record='{}' WHERE memory_id=?", (memory_id,))
            self.retriever.sync()
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
        experiences = [
            {**dict(row), "record": json.loads(row["record"])}
            for row in self.repo.conn.execute(
                "SELECT e.* FROM experiences e JOIN memories m ON m.id=e.memory_id WHERE m.deleted=0")
            if not scopes or row["scope"] in scopes
        ]
        return {"memories": memories, "experiences": experiences,
                "life": self.life.snapshot(OWNER_SCOPE),
                "state": self.repo.get_state(OWNER_SCOPE), "exported_at": self.clock()}

    def ping(self) -> Dict[str, Any]:
        return {"ok": True, "index_count": self.index.count()}
