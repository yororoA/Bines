"""Offline, manifest-bound recovery of a reviewed historical database branch.

Planning opens both databases read-only. Applying requires an unchanged logical
target snapshot and never reuses source message IDs, runs models, schedules jobs,
or replays state effects. Source text stays in the databases, not the plan.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .dependencies import SourceDependencies, message_range
from .models import content_hash, is_system_event
from .repository import MemoryRepository, consolidation_candidate

VERSION = "history-merge-v1"
BASELINE_TABLES = ("messages", "memories", "conversations", "identities", "identity_links",
                   "migration_ledger", "state", "jobs")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)


def validate_outputs(outputs, inputs=(), databases=()):
    """Reject output collisions before planning, copying, or applying anything."""
    protected = {Path(path).resolve() for path in inputs}
    for raw in databases:
        path = Path(raw)
        for base in (path.absolute(), path.resolve()):
            protected.update(Path(str(base) + suffix).resolve() for suffix in ("", "-wal", "-shm", "-journal"))
    seen = set()
    for raw in outputs:
        path = Path(raw)
        resolved = path.resolve()
        if path.exists() or path.is_symlink() or resolved in protected or resolved in seen:
            raise ValueError("artifact output collides with an existing file, input, database or sidecar")
        seen.add(resolved)


class _OfflineRepository(MemoryRepository):
    """Repository methods on an existing DB, with no constructor migrations."""
    def __init__(self, path):
        self.db_path, self.clock = Path(path), time.time
        self.conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=rw", uri=True,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("consolidation_candidate", 3, consolidation_candidate, deterministic=True)
        self.conn.execute("PRAGMA foreign_keys=ON")


@contextmanager
def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def verified_source(source, manifest):
    """Verify and copy the complete SQLite/WAL group before opening the copy."""
    source = Path(source).resolve()
    expected = {Path(row["path"]).name: row for row in manifest["files"]}
    if source.name not in expected:
        raise ValueError("source database is not in the reviewed manifest")
    names = {source.name + suffix for suffix in ("", "-wal", "-shm")}
    if set(expected) - names:
        raise ValueError("manifest must contain only one database and its sidecars")
    with tempfile.TemporaryDirectory(prefix="bines-history-source-") as directory:
        copied = Path(directory) / source.name
        for name in names:
            original = source.with_name(name)
            entry = expected.get(name)
            if original.exists() != bool(entry):
                raise ValueError("source SQLite sidecar set changed")
            if entry:
                if original.stat().st_size != entry["bytes"] or _file_hash(original) != entry["sha256"]:
                    raise ValueError("source SQLite/WAL manifest hash mismatch")
                destination = copied.with_name(name)
                shutil.copy2(original, destination)
                if _file_hash(destination) != entry["sha256"]:
                    raise ValueError("source changed while copying")
        with readonly(copied) as conn:
            yield conn


def fingerprint(conn):
    """Bind the plan to source-of-truth tables, including state and job queues."""
    result = hashlib.sha256()
    for table in BASELINE_TABLES:
        result.update(table.encode())
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not columns:
            raise ValueError("unsupported target schema: " + table)
        result.update(_json(columns).encode())
        # Stable across an SQLite backup and independent of page/WAL layout.
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY " + ",".join(columns)):
            result.update(_json(list(row)).encode())
    return result.hexdigest()


def _rows(conn, table):
    return {str(r["id"]): dict(r) for r in conn.execute("SELECT * FROM " + table)}


def _message_key(row):
    return (row["scope"], row["conversation_id"], row["role"], row["actor_id"],
            float(row["ts"]), content_hash(row["content"]))


def _memory_key(row):
    return (row["scope"], row["conversation_id"], row["kind"], row["subject_id"],
            row["day_key"], row["start_ts"], row["end_ts"], content_hash(row["content"]))


def _meta(row):
    value = json.loads(row.get("meta") or "{}")
    if not isinstance(value, dict):
        raise ValueError("invalid source metadata")
    return value


def _scope_type(scope):
    if scope.startswith("qq_group:") or ":group:" in scope:
        return "qq_group"
    if scope.startswith("qq_private:") or ":private:" in scope:
        return "qq_private"
    return scope.split(":", 1)[0]


def _tombstone(row, table):
    return bool(row.get("deleted")) or str(row.get("origin", "")).lower() in (
        "recalled", "withdrawn", "retracted", "deleted", "system")


def _same_deleted_source(old, current, table):
    key = _message_key if table == "messages" else _memory_key
    if key(old) == key(current):
        return True
    if old.get("source_ref") and old["source_ref"] == current.get("source_ref"):
        return True  # A tombstone may have erased the original content.
    erased = not str(current.get("content", "")).strip() or str(current["content"]).strip().lower() in (
        "[消息已撤回]", "[message recalled]", "[message deleted]", "[deleted]", "[withdrawn]")
    return erased and key(old)[:-1] == key(current)[:-1]


def _select(source, candidates):
    messages, memories = _rows(source, "messages"), _rows(source, "memories")
    selected = {"messages": set(), "memories": set()}
    for table, rows in (("messages", messages), ("memories", memories)):
        for item in candidates[table]:
            sid = str(item["id"])
            row = rows.get(sid)
            if not row or content_hash(row["content"]) != item["hash"]:
                raise ValueError("candidate content no longer matches audited source")
            if item.get("scope_type") and _scope_type(row["scope"]) != item["scope_type"]:
                raise ValueError("candidate scope differs from reviewed audit")
            selected[table].add(sid)
    evidence, parents, reasons = {}, {}, {}

    def visit(mid, visiting=()):
        if mid in evidence:
            return
        if mid in visiting or mid not in memories:
            raise ValueError("missing or cyclic source memory dependency")
        row, ids, refs = memories[mid], set(), set()
        meta = _meta(row)
        # Explicit evidence and legacy ranges are expanded *in the source DB*.
        for value in [*(meta.get("source_message_ids") or []), meta.get("fact_source_message_id")]:
            if value is not None:
                ids.add(str(value))
        bounds = message_range(meta.get("message_range"))
        if meta.get("message_range") is not None and not bounds:
            raise ValueError("invalid source message range")
        if bounds:
            if not row["conversation_id"]:
                raise ValueError("source message range has no conversation")
            ids.update(sid for sid, m in messages.items()
                       if m["scope"] == row["scope"] and m["conversation_id"] == row["conversation_id"]
                       and bounds[0] <= int(sid) <= bounds[1])
        ids.update(sid for sid, m in messages.items() if m["episode_id"] == mid)
        for ref in meta.get("source_message_refs") or []:
            matches = [sid for sid, m in messages.items() if m["source_ref"] == ref]
            if len(matches) != 1:
                raise ValueError("unresolved source message reference")
            ids.update(matches)
        for ref in [*(meta.get("source_memory_ids") or []), *(meta.get("episode_ids") or []), meta.get("episode_id")]:
            if ref:
                ref = str(ref)
                if ref not in memories or memories[ref]["scope"] != row["scope"]:
                    raise ValueError("missing or cross-scope parent memory")
                refs.add(ref)
                visit(ref, (*visiting, mid))
        for sid in ids:
            m = messages.get(sid)
            if not m or m["scope"] != row["scope"] or (row["conversation_id"] and m["conversation_id"] != row["conversation_id"]):
                raise ValueError("missing or cross-scope source message")
        inherited = set().union(*(evidence[ref] for ref in refs)) if refs else set()
        evidence[mid] = ids | inherited
        parents[mid] = refs
        selected["messages"].update(evidence[mid])
        selected["memories"].add(mid)
        reason = None
        if row["deleted"] or not evidence[mid]:
            reason = "deleted_or_missing_evidence"
        if row["kind"] == "semantic":
            # Historical summaries are not enough to invent an attribution.
            proof = [messages[sid] for sid in evidence[mid] if messages[sid]["role"] == "user"
                     and messages[sid]["actor_id"] == row["subject_id"]
                     and row["content"].strip() in messages[sid]["content"]]
            if not proof:
                reason = "fact_requires_attributed_verbatim_evidence"
        reasons[mid] = reason

    for mid in sorted(selected["memories"]):
        visit(mid)
    for sid in selected["messages"]:
        row = messages[sid]
        if row["role"] not in ("user", "assistant", "observed") or is_system_event(row["content"]):
            raise ValueError("candidate includes unsupported role or system event")
        if row["origin"] in ("recalled", "withdrawn", "retracted", "deleted", "system"):
            raise ValueError("candidate includes a withdrawn source")
    return messages, memories, selected, evidence, parents, reasons


def _check_identities(source, target, messages, selected):
    conversations = _rows(source, "conversations")
    for sid in selected["messages"]:
        row = messages[sid]
        conv = conversations.get(row["conversation_id"])
        if not conv or conv["scope"] != row["scope"]:
            raise ValueError("source message/conversation scope mismatch")
        current = target.execute("SELECT * FROM conversations WHERE id=?", (conv["id"],)).fetchone()
        if current and (current["scope"], current["channel"]) != (conv["scope"], conv["channel"]):
            raise ValueError("target conversation identity changed; explicit identity review required")
        if row["scope"] == "owner" and conv["channel"] == "qq_private":
            external = conv["id"].split(":private:")[-1] if ":private:" in conv["id"] else conv["id"].removeprefix("qq_private:")
            for conn in (source, target):
                link = conn.execute("SELECT identity_id FROM identity_links WHERE channel='qq' AND external_id=?", (external,)).fetchone()
                if not link or link[0] != "owner":
                    raise ValueError("owner private identity is not independently linked in both databases")
        elif row["scope"] == "owner" and conv["channel"] != "local":
            raise ValueError("group or unknown channel cannot restore owner memories")
        if conv["channel"] == "qq_group" and (row["scope"] != conv["id"] or _scope_type(row["scope"]) != "qq_group"):
            raise ValueError("invalid group scope")
    return conversations


def _plan(source, target, candidates, lineage):
    messages, memories, selected, evidence, parents, reasons = _select(source, candidates)
    _check_identities(source, target, messages, selected)
    result = {"messages": [], "memories": []}
    for table, rows, key in (("messages", messages, _message_key), ("memories", memories, _memory_key)):
        current = _rows(target, table)
        by_key = {}
        for row in current.values():
            if table == "memories" and row["deleted"]:
                continue
            if table == "messages" and row["origin"] in ("recalled", "withdrawn", "retracted", "deleted", "system"):
                continue
            by_key.setdefault(key(row), str(row["id"]))
        selected_keys = {}
        for sid in sorted(selected[table], key=lambda x: int(x) if table == "messages" else x):
            row = rows[sid]
            entry = target.execute("SELECT * FROM migration_ledger WHERE source=? AND source_id=?",
                                   (lineage, table + ":" + sid)).fetchone()
            for existing in current.values():
                own_quarantine = (table == "memories" and entry and reasons[sid]
                                  and existing["id"] == entry["target_id"]
                                  and existing["source_ref"] == lineage + ":memory:" + sid)
                if not own_quarantine and _tombstone(existing, table) and _same_deleted_source(row, existing, table):
                    # Reject the whole closure; descendants must never reactivate
                    # evidence deliberately removed from the current database.
                    raise ValueError("target contains a deleted or withdrawn source; dependent recovery requires review")
            target_id = str(entry["target_id"]) if entry else by_key.get(key(row))
            if entry and (entry["target_table"] != table or entry["content_hash"] != content_hash(row["content"])
                          or target_id not in current or key(current[target_id]) != key(row)):
                raise ValueError("existing recovery ledger target changed or disappeared")
            duplicate_of = selected_keys.get(key(row)) if not target_id else None
            item = {"source_id": sid, "content_hash": content_hash(row["content"]), "scope": row["scope"],
                    "conversation_id": row["conversation_id"], "target_id": target_id,
                    "duplicate_of": duplicate_of,
                    "action": "existing" if entry else "deduplicate" if target_id else "duplicate_source" if duplicate_of else "insert"}
            selected_keys.setdefault(key(row), sid)
            if table == "memories":
                item.update(source_message_ids=sorted(evidence[sid], key=int), source_memory_ids=sorted(parents[sid]),
                            quarantine_reason=reasons[sid], kind=row["kind"])
            else:
                item.update(role=row["role"], day_key=row["day_key"])
            result[table].append(item)
    result["summary"] = {
        "messages_selected": len(result["messages"]),
        "messages_insert": sum(x["action"] == "insert" for x in result["messages"]),
        "dependency_messages_added": len(selected["messages"]) - len(candidates["messages"]),
        "memories_insert": sum(x["action"] == "insert" for x in result["memories"]),
        "memories_quarantined": sum(bool(x["quarantine_reason"]) for x in result["memories"]),
    }
    return result


def make_plan(source, target, manifest, candidates):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or source.samefile(target):
        raise ValueError("source and target must differ")
    # SHM is an ephemeral reader lock map, not branch identity; still validate it.
    lineage = VERSION + ":" + _digest([r for r in manifest["files"] if not r["path"].endswith("-shm")])
    with verified_source(source, manifest) as src, readonly(target) as dst:
        plan = {"version": VERSION, "source": str(source), "target": str(target), "manifest": manifest,
                "candidates": candidates, "lineage": lineage, "target_baseline": fingerprint(dst),
                **_plan(src, dst, candidates, lineage)}
    plan["plan_hash"] = _digest(plan)
    return plan


def _insert(conn, table, row):
    names = list(row)
    return conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(row.values()))


def _check_recovered_links(conn, actions, lineage):
    """An idempotent retry also verifies provenance and remapped dependencies."""
    message_map = {item["source_id"]: int(item["target_id"]) for item in actions["messages"]}
    memory_map = {item["source_id"]: item["target_id"] for item in actions["memories"]}
    for item in actions["memories"]:
        row = dict(conn.execute("SELECT * FROM memories WHERE id=?", (item["target_id"],)).fetchone())
        if row["source_ref"] != lineage + ":memory:" + item["source_id"]:
            continue  # Existing content deduplicated without changing its metadata.
        meta = _meta(row)
        if (set(meta.get("source_message_ids") or []) != {message_map[x] for x in item["source_message_ids"]}
                or set(meta.get("source_memory_ids") or []) != {memory_map[x] for x in item["source_memory_ids"]}
                or bool(row["deleted"]) != bool(item["quarantine_reason"])
                or meta.get("history_merge", {}).get("lineage") != lineage):
            raise ValueError("recovered memory provenance or dependencies changed")
    for item in actions["messages"]:
        row = conn.execute("SELECT origin,source_ref FROM messages WHERE id=?", (item["target_id"],)).fetchone()
        if row["source_ref"] == lineage + ":message:" + item["source_id"] and row["origin"] != "archived":
            raise ValueError("recovered message is no longer historical")


def apply_plan(plan, target=None):
    """Apply to an offline target. Callers must stop writers or use stage_plan."""
    if plan.get("version") != VERSION or _digest({k: v for k, v in plan.items() if k != "plan_hash"}) != plan.get("plan_hash"):
        raise ValueError("reviewed plan was changed")
    target = Path(target or plan["target"]).resolve()
    if target == Path(plan["source"]).resolve() or target.samefile(plan["source"]):
        raise ValueError("cannot apply to source backup")
    # Validate source and target before opening any writable connection.
    with verified_source(plan["source"], plan["manifest"]) as source:
        with readonly(target) as before:
            fresh = _plan(source, before, plan["candidates"], plan["lineage"])
            complete = all(x["action"] == "existing" for table in ("messages", "memories") for x in fresh[table])
            if complete:
                _check_recovered_links(before, fresh, plan["lineage"])
                return {"messages_inserted": 0, "memories_inserted": 0, "already_applied": True}
            if fingerprint(before) != plan["target_baseline"]:
                raise ValueError("target baseline changed; generate and review a new plan")
            if any(fresh[k] != plan[k] for k in ("messages", "memories", "summary")):
                raise ValueError("reviewed actions no longer match")
        old_messages, old_memories = _rows(source, "messages"), _rows(source, "memories")
        conversations = _rows(source, "conversations")
        repo = _OfflineRepository(target)
        try:
            with repo.transaction():
                conn = repo.conn
                if fingerprint(conn) != plan["target_baseline"]:
                    raise ValueError("target changed before write lock")
                protected = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in ("state", "jobs")}
                message_map, memory_map = {}, {}
                inserted_messages, inserted_memories = [], []
                for item in plan["memories"]:
                    memory_map[item["source_id"]] = item["target_id"] or (
                        memory_map[item["duplicate_of"]] if item["duplicate_of"] else uuid.uuid4().hex)
                for item in plan["messages"]:
                    sid, row = item["source_id"], dict(old_messages[item["source_id"]])
                    if item["duplicate_of"]:
                        message_map[sid] = message_map[item["duplicate_of"]]
                        continue
                    if item["target_id"]:
                        message_map[sid] = int(item["target_id"])
                        continue
                    conv = conversations[row["conversation_id"]]
                    if not conn.execute("SELECT 1 FROM conversations WHERE id=?", (conv["id"],)).fetchone():
                        _insert(conn, "conversations", conv)
                    row.pop("id")
                    row["origin"] = "archived"
                    row["source_ref"] = plan["lineage"] + ":message:" + sid
                    row["episode_id"] = memory_map.get(row["episode_id"])
                    message_map[sid] = _insert(conn, "messages", row).lastrowid
                    inserted_messages.append(message_map[sid])
                for item in plan["memories"]:
                    sid, row = item["source_id"], dict(old_memories[item["source_id"]])
                    if item["target_id"] or item["duplicate_of"]:
                        continue
                    meta = _meta(row)
                    for field in ("message_range", "source_message_refs", "source_experience_refs", "experience_ref"):
                        meta.pop(field, None)
                    meta["source_message_ids"] = [message_map[x] for x in item["source_message_ids"]]
                    meta["source_memory_ids"] = [memory_map[x] for x in item["source_memory_ids"]]
                    if meta.get("episode_id"):
                        meta["episode_id"] = memory_map[meta["episode_id"]]
                    if meta.get("episode_ids"):
                        meta["episode_ids"] = [memory_map[x] for x in meta["episode_ids"]]
                    if meta.get("fact_source_message_id") is not None:
                        meta["fact_source_message_id"] = message_map[str(meta["fact_source_message_id"])]
                    meta.update(historical=True, history_merge={"lineage": plan["lineage"], "source_memory_id": sid,
                                "source_content_hash": item["content_hash"], "original_origin": row["origin"],
                                "review_status": item["quarantine_reason"] or "source_evidence_verified"})
                    row.update(id=memory_map[sid], meta=_json(meta), origin="history_merge",
                               source_ref=plan["lineage"] + ":memory:" + sid, indexed_at=None,
                               deleted=int(bool(item["quarantine_reason"])), updated_at=time.time())
                    _insert(conn, "memories", row)
                    inserted_memories.append(memory_map[sid])
                for table, mapping in (("messages", message_map), ("memories", memory_map)):
                    for item in plan[table]:
                        if item["action"] == "existing":
                            continue
                        conn.execute("INSERT INTO migration_ledger VALUES(?,?,?,?,?,?)",
                                     (plan["lineage"], table + ":" + item["source_id"], table,
                                      str(mapping[item["source_id"]]), item["content_hash"], time.time()))
                # Native metadata projection, without graph jobs or model calls.
                SourceDependencies(repo).sync()
                for table, original in protected.items():
                    if original != [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]:
                        raise ValueError("historical recovery changed state or background jobs")
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("restored target has foreign-key violations")
                result = {"messages_inserted": len(inserted_messages), "memories_inserted": len(inserted_memories),
                          "memories_quarantined": plan["summary"]["memories_quarantined"],
                          "message_ids": inserted_messages, "memory_ids": inserted_memories,
                          "message_mapping": message_map, "memory_mapping": memory_map,
                          "state_and_jobs_unchanged": True, "already_applied": False,
                          "index_action": "Reconcile retrieval keys/index offline or through the Memory Server after review."}
            return result
        finally:
            repo.close()


def stage_plan(plan, output):
    """SQLite backup API gives a consistent copy without writing the live target."""
    output = Path(output)
    validate_outputs([output], databases=[plan["source"], plan["target"]])
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with readonly(plan["target"]) as source:
        if fingerprint(source) != plan["target_baseline"]:
            raise ValueError("target baseline changed before staging")
        stage = sqlite3.connect(str(output))
        try:
            output.chmod(0o600)
            source.backup(stage)
        finally:
            stage.close()
    return apply_plan(plan, output)
