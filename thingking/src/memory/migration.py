"""One-way, idempotent import of the legacy memory files into the SQLite store.

Legacy sources (all under ``thingking/src``):
- ``memory_short.json``      -> messages of the local owner conversation (still pending consolidation)
- ``memory_dynamic.json``    -> owner state
- ``memory_permanent.json``  -> owner semantic memories (``user_profile``); core_rules/facts now live in persona.yaml
- ``qq_msg_buffer.json`` / ``qq_msg_private_buffer.json`` and Chroma ``qq_history_store`` -> QQ messages
- Chroma ``chat_memory`` (episode_summary / raw_conversation), ``summary_buffer``, ``long_term_diary`` -> memories

Every imported item is recorded in ``migration_ledger`` with its source id and content hash, so
re-running the migration never duplicates data.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import scope_policy
from .models import (
    CHANNEL_LOCAL,
    KIND_DIARY,
    KIND_EPISODE,
    KIND_EXCERPT,
    KIND_SEMANTIC,
    LOCAL_CONVERSATION,
    ORIGIN_LEGACY,
    OWNER_ACTOR,
    OWNER_SCOPE,
    ROLE_ASSISTANT,
    ROLE_OBSERVED,
    ROLE_USER,
    content_hash,
    day_key,
)
from .channels import qq_envelope
from .repository import MemoryRepository

LEGACY_FILES = (
    "memory_short.json",
    "memory_dynamic.json",
    "memory_permanent.json",
    "qq_msg_buffer.json",
    "qq_msg_private_buffer.json",
)
LEGACY_CHROMA_DIR = "chroma_db_layered"
LEGACY_CONSOLIDATED = "legacy"

_USER_PREFIX = re.compile(r"^\[QQ(?:群消息|私聊)\]\[([^\]]*)\]:\s*", re.S)
_BOT_PREFIX = re.compile(r"^\[QQ(?:群回复|私聊回复)\]\[Bot\]:\s*", re.S)
_ADMIN_SUFFIX = re.compile(r"\s*\(管理员\)$")
_SUMMARY_PREFIX = re.compile(r"^【QQ会话摘要 - (group|private)_([^】]+)】\s*")


@dataclass
class MigrationReport:
    imported: Dict[str, int] = field(default_factory=dict)
    skipped: Dict[str, int] = field(default_factory=dict)
    source_counts: Dict[str, int] = field(default_factory=dict)

    def count(self, source: str, imported: bool) -> None:
        bucket = self.imported if imported else self.skipped
        bucket[source] = bucket.get(source, 0) + 1

    def to_dict(self) -> Dict[str, Any]:
        return {"imported": self.imported, "skipped": self.skipped, "source_counts": self.source_counts}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_legacy(source_dir: Path, backup_root: Path) -> Path:
    """Copy every legacy file into a timestamped backup directory and write a checksum manifest."""
    target = backup_root / time.strftime("%Y%m%d_%H%M%S")
    target.mkdir(parents=True, exist_ok=False)
    manifest: Dict[str, str] = {}
    for name in LEGACY_FILES:
        path = source_dir / name
        if path.exists():
            shutil.copy2(path, target / name)
            manifest[name] = _sha256_file(path)
    chroma = source_dir / LEGACY_CHROMA_DIR
    if chroma.exists():
        shutil.copytree(chroma, target / LEGACY_CHROMA_DIR)
        for file in sorted((target / LEGACY_CHROMA_DIR).rglob("*")):
            if file.is_file():
                manifest[str(file.relative_to(target)).replace("\\", "/")] = _sha256_file(file)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def verify_snapshot(snapshot_dir: Path) -> List[str]:
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    return [name for name, digest in manifest.items() if _sha256_file(snapshot_dir / name) != digest]


def read_legacy_chroma(chroma_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Read documents + metadata straight from Chroma's SQLite file (read-only, no embeddings needed)."""
    db = chroma_dir / "chroma.sqlite3"
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT c.name, e.embedding_id, m.key, m.string_value, m.int_value, m.float_value, m.bool_value "
            "FROM embeddings e JOIN segments s ON e.segment_id = s.id JOIN collections c ON s.collection = c.id "
            "LEFT JOIN embedding_metadata m ON m.id = e.id"
        ).fetchall()
    finally:
        conn.close()
    docs: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for collection, doc_id, key, s_val, i_val, f_val, b_val in rows:
        entry = docs.setdefault((collection, doc_id), {"id": doc_id, "document": "", "metadata": {}})
        if key is None:
            continue
        value = next((v for v in (s_val, i_val, f_val, b_val) if v is not None), None)
        if key == "chroma:document":
            entry["document"] = value or ""
        else:
            entry["metadata"][key] = value
    out: Dict[str, List[Dict[str, Any]]] = {}
    for (collection, _), entry in docs.items():
        out.setdefault(collection, []).append(entry)
    for entries in out.values():
        entries.sort(key=lambda e: e["id"])
    return out


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_qq_text(content: str) -> Tuple[str, str, str]:
    """Return (role, sender_name, text) from a legacy QQ buffer/log line."""
    text = (content or "").strip()
    bot = _BOT_PREFIX.match(text)
    if bot:
        return ROLE_ASSISTANT, "", text[bot.end():].strip()
    user = _USER_PREFIX.match(text)
    if user:
        return ROLE_USER, _ADMIN_SUFFIX.sub("", user.group(1)).strip(), text[user.end():].strip()
    return ROLE_OBSERVED, "", text


def _qq_ref(group_id: Any, user_id: Any, ts: Optional[float], text: str) -> str:
    key = f"{group_id or ''}|{user_id or ''}|{int(ts or 0)}|{content_hash(text)}"
    return "qq:" + hashlib.sha1(key.encode("utf-8")).hexdigest()


class LegacyMigrator:
    def __init__(self, repo: MemoryRepository, owner_qq_ids: Iterable[str], day_start_hour: int = 4):
        self.repo = repo
        self.owner_qq_ids = {str(q) for q in owner_qq_ids}
        self.day_start_hour = day_start_hour
        self.report = MigrationReport()

    def _day(self, ts: Optional[float]) -> str:
        return day_key(ts, self.day_start_hour)

    def _ledger(self, source: str, source_id: str, table: str, target_id: Any, text: str) -> None:
        self.repo.ledger_add(source, source_id, table, str(target_id), content_hash(text))

    def _already(self, source: str, source_id: str) -> bool:
        if self.repo.ledger_get(source, source_id):
            self.report.count(source, imported=False)
            return True
        return False

    def run(self, source_dir: Path) -> MigrationReport:
        """Import from a directory that has the legacy layout (normally the verified snapshot)."""
        self.repo.upsert_identity(OWNER_ACTOR, "owner")
        for qq in self.owner_qq_ids:
            self.repo.link_identity("qq", qq, OWNER_ACTOR)
        with self.repo.transaction():
            self._import_permanent(source_dir / "memory_permanent.json")
            self._import_dynamic(source_dir / "memory_dynamic.json")
            self._import_short(source_dir / "memory_short.json")
            for name in ("qq_msg_buffer.json", "qq_msg_private_buffer.json"):
                self._import_qq_buffer(source_dir / name, f"file:{name}")
            chroma = read_legacy_chroma(source_dir / LEGACY_CHROMA_DIR)
            self._import_chroma(chroma)
        return self.report

    # ---------- JSON files ----------
    def _import_permanent(self, path: Path) -> None:
        if not path.exists():
            return
        profile = (json.loads(path.read_text(encoding="utf-8")) or {}).get("user_profile") or {}
        facts: List[Tuple[str, str]] = []
        if profile.get("name"):
            facts.append(("name", f"管理员的名字/昵称：{profile['name']}"))
        if profile.get("role"):
            facts.append(("role", f"管理员在系统中的身份：{profile['role']}"))
        for i, part in enumerate(re.split(r"[,，]\s*", str(profile.get("preferences") or ""))):
            part = part.strip().rstrip("。")
            if part:
                facts.append((f"preferences:{i}", f"关于管理员：{part}"))
        source = "file:memory_permanent.json"
        self.report.source_counts[source] = len(facts)
        for key, text in facts:
            if self._already(source, key):
                continue
            memory_id = self.repo.add_memory(
                KIND_SEMANTIC, OWNER_SCOPE, text, subject_id=OWNER_ACTOR, importance=8,
                origin=ORIGIN_LEGACY, source_ref=f"{source}:{key}", meta={"legacy_source": source},
            )
            self._ledger(source, key, "memories", memory_id, text)
            self.report.count(source, imported=True)

    def _import_dynamic(self, path: Path) -> None:
        if not path.exists():
            return
        source = "file:memory_dynamic.json"
        self.report.source_counts[source] = 1
        if self._already(source, "state"):
            return
        state = json.loads(path.read_text(encoding="utf-8")) or {}
        state.pop("current_time", None)
        current = self.repo.get_state(OWNER_SCOPE)
        merged = {**state, **(current["data"] or {})}
        self.repo.put_state(OWNER_SCOPE, merged)
        self._ledger(source, "state", "state", OWNER_SCOPE, json.dumps(state, ensure_ascii=False, sort_keys=True))
        self.report.count(source, imported=True)

    def _import_short(self, path: Path) -> None:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8")) or []
        rounds = data if data and isinstance(data[0], list) else [[m] for m in data]
        source = "file:memory_short.json"
        self.repo.ensure_conversation(LOCAL_CONVERSATION, CHANNEL_LOCAL, OWNER_SCOPE)
        items = [(i, j, m) for i, rnd in enumerate(rounds) for j, m in enumerate(rnd) if isinstance(m, dict)]
        self.report.source_counts[source] = len(items)
        for i, j, message in items:
            source_id = f"{i}:{j}"
            if self._already(source, source_id):
                continue
            role = ROLE_ASSISTANT if message.get("role") == "assistant" else ROLE_USER
            text = str(message.get("content") or "")
            match = re.match(r"^\[\d{4}/\d{1,2}/\d{1,2}(?: \d{1,2}:\d{2})?\]:\s*\[(.*)\]\s*$", text, re.S)
            if match:
                text = match.group(1)
            ts = _float(message.get("timestamp")) or time.time()
            message_id = self.repo.add_message(
                LOCAL_CONVERSATION, OWNER_SCOPE, role, text, ts, self._day(ts),
                actor_id=OWNER_ACTOR if role == ROLE_USER else "assistant", origin=ORIGIN_LEGACY,
                source_ref=f"{source}:{source_id}",
            )
            self._ledger(source, source_id, "messages", message_id, text)
            self.report.count(source, imported=True)

    def _store_qq_message(self, source: str, source_id: str, raw_content: str, meta: Dict[str, Any]) -> None:
        if self._already(source, source_id):
            return
        role, name, text = _parse_qq_text(raw_content)
        if not text:
            self._ledger(source, source_id, "skipped", "", raw_content)
            self.report.count(source, imported=False)
            return
        group_id, user_id = meta.get("group_id"), meta.get("user_id")
        ts = _float(meta.get("timestamp")) or time.time()
        if role == ROLE_OBSERVED and not group_id:
            role = ROLE_USER
        sender = name or ("" if str(meta.get("sender") or "") in ("User", "Self(Bot)") else str(meta.get("sender") or ""))
        envelope = qq_envelope({"user_id": user_id, "group_id": group_id, "sender_name": sender}, text, ts)
        resolved = scope_policy.resolve(envelope, self.repo.lookup_identity)
        self.repo.ensure_conversation(resolved.conversation_id, resolved.channel_type, resolved.scope)
        message_id = self.repo.add_message(
            resolved.conversation_id, resolved.scope, role, text, ts, self._day(ts),
            actor_id="assistant" if role == ROLE_ASSISTANT else resolved.actor_id,
            actor_name="" if role == ROLE_ASSISTANT else sender, origin=ORIGIN_LEGACY,
            episode_id=LEGACY_CONSOLIDATED, source_ref=_qq_ref(group_id, user_id, ts, text),
        )
        self._ledger(source, source_id, "messages", message_id, text)
        self.report.count(source, imported=True)

    def _import_qq_buffer(self, path: Path, source: str) -> None:
        if not path.exists():
            return
        messages = (json.loads(path.read_text(encoding="utf-8")) or {}).get("messages") or []
        self.report.source_counts[source] = len(messages)
        for index, message in enumerate(messages):
            self._store_qq_message(source, str(index), str(message.get("content") or ""), message.get("meta") or {})

    # ---------- Chroma collections ----------
    def _import_chroma(self, collections: Dict[str, List[Dict[str, Any]]]) -> None:
        for entry in collections.get("qq_history_store", []):
            self._store_qq_message("chroma:qq_history_store", entry["id"], entry["document"], entry["metadata"])
        self.report.source_counts["chroma:qq_history_store"] = len(collections.get("qq_history_store", []))

        for name, handler in (
            ("chat_memory", self._chat_memory_entry),
            ("summary_buffer", self._summary_buffer_entry),
            ("long_term_diary", self._diary_entry),
        ):
            source = f"chroma:{name}"
            entries = collections.get(name, [])
            self.report.source_counts[source] = len(entries)
            for entry in entries:
                if self._already(source, entry["id"]):
                    continue
                text = (entry["document"] or "").strip()
                if not text:
                    self._ledger(source, entry["id"], "skipped", "", "")
                    self.report.count(source, imported=False)
                    continue
                memory_id = handler(source, entry, text)
                self._ledger(source, entry["id"], "memories", memory_id, text)
                self.report.count(source, imported=True)

    def _memory(self, source: str, entry: Dict[str, Any], text: str, kind: str, scope: str, **fields: Any) -> str:
        meta = entry["metadata"]
        ts = _float(meta.get("end_time")) or _float(meta.get("timestamp"))
        default_importance = fields.pop("importance", 5)
        return self.repo.add_memory(
            kind, scope, text,
            importance=_float(meta.get("importance_score")) or default_importance,
            start_ts=_float(meta.get("start_time")), end_ts=ts,
            day_key=meta.get("day_key") or (self._day(ts) if ts else None),
            tags=[t for t in str(meta.get("tags") or "").split(",") if t],
            meta={"legacy_source": source, "legacy_type": meta.get("type")},
            origin=ORIGIN_LEGACY, source_ref=f"{source}:{entry['id']}", created_at=ts, **fields,
        )

    def _chat_memory_entry(self, source: str, entry: Dict[str, Any], text: str) -> str:
        kind = KIND_EPISODE if entry["metadata"].get("type") == "episode_summary" else KIND_EXCERPT
        return self._memory(source, entry, text, kind, OWNER_SCOPE)

    def _summary_buffer_entry(self, source: str, entry: Dict[str, Any], text: str) -> str:
        scope, conversation_id = OWNER_SCOPE, None
        match = _SUMMARY_PREFIX.match(text)
        if match:
            channel, external = match.groups()
            if channel == "group":
                scope = conversation_id = f"qq_group:{external}"
            elif external not in self.owner_qq_ids:
                scope = conversation_id = f"qq_private:{external}"
            else:
                conversation_id = f"qq_private:{external}"
        return self._memory(source, entry, text, KIND_EPISODE, scope, conversation_id=conversation_id)

    def _diary_entry(self, source: str, entry: Dict[str, Any], text: str) -> str:
        return self._memory(source, entry, text, KIND_DIARY, OWNER_SCOPE, importance=8)


def verify_migration(repo: MemoryRepository, report_counts: Dict[str, int]) -> Dict[str, Any]:
    """Check every ledger entry still points at a row whose content hash matches, and counts line up."""
    problems: List[str] = []
    per_source: Dict[str, int] = {}
    for entry in repo.ledger_entries():
        per_source[entry["source"]] = per_source.get(entry["source"], 0) + 1
        if entry["target_table"] == "memories":
            row = repo.get_memory(entry["target_id"])
            text = row["content"] if row else None
        elif entry["target_table"] == "messages":
            row = repo.get_message(int(entry["target_id"]))
            text = row["content"] if row else None
        else:
            continue
        if text is None:
            problems.append(f"{entry['source']}:{entry['source_id']} -> missing {entry['target_table']} {entry['target_id']}")
        elif content_hash(text) != entry["content_hash"]:
            problems.append(f"{entry['source']}:{entry['source_id']} -> content hash mismatch")
    for source, expected in report_counts.items():
        if per_source.get(source, 0) < expected:
            problems.append(f"{source}: ledger has {per_source.get(source, 0)} of {expected} items")
    return {"ledger_per_source": per_source, "problems": problems}
