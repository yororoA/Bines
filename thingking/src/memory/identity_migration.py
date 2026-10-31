"""Move only evidence attributable to one private conversation when identity links change."""
import json
import uuid

from .associations import digest


def migrate(service, envelope, old_scope):
    repo, conn, graph = service.repo, service.repo.conn, service.graph
    conversation, new_scope = envelope.conversation_id, envelope.scope
    old_actor = "owner" if old_scope == "owner" else "qq:" + envelope.external_user_id
    new_actor = envelope.actor_id
    repo.temporal.migrate_fact_key(old_scope, old_actor, new_scope, new_actor)
    messages = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE conversation_id=?", (conversation,))]
    message_ids = {m["id"] for m in messages}
    memories = {r[0]: repo.get_memory(r[0]) for r in conn.execute("SELECT id FROM memories WHERE scope=?", (old_scope,)).fetchall()}
    ownership = {}

    def owners(mid, seen=()):
        if mid in ownership:
            return ownership[mid]
        if mid in seen or mid not in memories:
            return {None}
        memory = memories[mid]
        meta, result = memory["meta"], set()
        if meta.get("identity_conversation_id"):
            result.add(meta["identity_conversation_id"])
        if memory["conversation_id"]:
            result.add(memory["conversation_id"])
        ids = set(meta.get("source_message_ids") or [])
        if meta.get("fact_source_message_id"):
            ids.add(meta["fact_source_message_id"])
        for ref in meta.get("source_message_refs") or []:
            row = conn.execute("SELECT id FROM messages WHERE source_ref=?", (ref,)).fetchone()
            if row:
                ids.add(row[0])
            else:
                result.add(None)
        for message_id in ids:
            message = repo.get_message(message_id)
            result.add(message["conversation_id"] if message else None)
        linked = set(meta.get("source_memory_ids") or []) | set(meta.get("episode_ids") or [])
        if meta.get("episode_id"):
            linked.add(meta["episode_id"])
        for source in linked:
            result.update(owners(source, (*seen, mid)))
        ownership[mid] = result or ({conversation} if old_scope != "owner" else {None})
        return ownership[mid]

    moved = {mid for mid in memories if owners(mid) == {conversation}}
    uncertain = {mid for mid in memories if conversation in owners(mid) and mid not in moved}
    marker = "identity:" + uuid.uuid4().hex
    affected_nodes = [dict(r) for r in conn.execute("SELECT * FROM association_nodes WHERE scope=?", (old_scope,))
                      if r["memory_id"] in moved or message_ids.intersection(json.loads(r["message_ids"]))]
    node_ids = {n["id"] for n in affected_nodes}
    invalidated_nodes = node_ids | {node["id"] for mid in uncertain if (node := graph.for_memory(mid))}
    for nid in invalidated_nodes:
        # Reasons may quote evidence from several members. Retiring only one
        # edge could leave its private text reachable through another member.
        chains = [r[0] for r in conn.execute("SELECT chain_id FROM chain_memberships WHERE node_id=?", (nid,))]
        for chain_id in chains:
            conn.execute("UPDATE association_edges SET confidence=0 WHERE chain_id=?", (chain_id,))
            conn.execute("DELETE FROM chain_memberships WHERE chain_id=?", (chain_id,))
            conn.execute("DELETE FROM association_reason_fts WHERE id=?", (chain_id,))
    node_map = {n["id"]: "n:" + digest([new_scope, n["source_type"], n["source_ref"]])[:32]
                for n in affected_nodes}
    experience_refs = [r[0] for r in conn.execute("SELECT source_ref,memory_id FROM experiences WHERE scope=?", (old_scope,))
                       if r[1] in moved]

    # State effects retain physical message identity across scope changes. Never
    # replay a completed relationship increment simply because its owner changed.
    service.state_effects.migrate_conversation(conversation, old_scope, new_scope)
    conn.execute("UPDATE conversations SET scope=? WHERE id=?", (new_scope, conversation))
    conn.execute("UPDATE messages SET scope=?,actor_id=CASE WHEN actor_id=? THEN ? ELSE actor_id END "
                 "WHERE conversation_id=?", (new_scope, old_actor, new_actor, conversation))
    for mid in moved:
        memory = memories[mid]
        subject = new_actor if memory["subject_id"] == old_actor else memory["subject_id"]
        meta = {**memory["meta"], "identity_conversation_id": conversation}
        conn.execute("UPDATE memories SET scope=?,subject_id=?,meta=?,indexed_at=NULL WHERE id=?",
                     (new_scope, subject, json.dumps(meta, ensure_ascii=False), mid))
        fact = conn.execute("SELECT * FROM fact_versions WHERE memory_id=?", (mid,)).fetchone()
        if fact:
            fact_subject = new_actor if fact["subject"] == old_actor else fact["subject"]
            conflict = conn.execute("SELECT 1 FROM fact_versions WHERE scope=? AND subject=? AND predicate=? "
                                    "AND valid_from=? AND memory_id!=?", (new_scope, fact_subject,
                                    fact["predicate"], fact["valid_from"], mid)).fetchone()
            if conflict:
                uncertain.add(mid)
            else:
                conn.execute("UPDATE fact_versions SET scope=?,subject=? WHERE memory_id=?", (new_scope, fact_subject, mid))
        conn.execute("UPDATE experiences SET scope=? WHERE memory_id=?", (new_scope, mid))

    # Mixed summaries and diaries cannot be assigned to one actor. Keep them for
    # audit, hide them from recall, and release their source messages for rebuilding.
    for mid in uncertain:
        memory = repo.get_memory(mid)
        repo.update_memory(mid, meta={**memory["meta"], "repair_pending": marker,
                                     "identity_rebuild_pending": conversation})
        conn.execute("UPDATE messages SET episode_id=NULL WHERE episode_id=?", (mid,))
        for source in memory["meta"].get("source_message_ids") or []:
            conn.execute("UPDATE messages SET episode_id=NULL WHERE id=?", (source,))
        node = graph.for_memory(mid)
        if node:
            graph.invalidate(node["id"])
            conn.execute("UPDATE association_edges SET confidence=0 WHERE source_node=? OR target_node=?", (node["id"], node["id"]))

    # Old graph scores and reason contexts are scope-specific. Rebuild the nodes
    # with fresh citations instead of reusing authority from another scope.
    for node in affected_nodes:
        nid = node["id"]
        conn.execute("UPDATE association_edges SET confidence=0 WHERE source_node=? OR target_node=?", (nid, nid))
        conn.execute("DELETE FROM chain_memberships WHERE node_id=?", (nid,))
        conn.execute("DELETE FROM association_message_nodes WHERE node_id=?", (nid,))
        conn.execute("DELETE FROM association_nodes WHERE id=?", (nid,))
        for correction in conn.execute("SELECT seq,operation_id FROM memory_corrections WHERE scope=? AND node_id=? AND kind='fact'",
                                       (old_scope, nid)).fetchall():
            conflict = conn.execute("SELECT 1 FROM memory_corrections WHERE scope=? AND operation_id=?",
                                    (new_scope, correction["operation_id"])).fetchone()
            if conflict:
                # Do not silently drop a correction when independent namespaces
                # reused an operation identifier.
                raise ValueError("identity migration has conflicting correction identifiers")
            conn.execute("UPDATE memory_corrections SET scope=?,node_id=? WHERE seq=?", (new_scope, node_map[nid], correction["seq"]))
        graph.invalidate(nid)

    for row in conn.execute("SELECT * FROM jobs WHERE scope=?", (old_scope,)).fetchall():
        payload = json.loads(row["payload"])
        belongs = (row["conversation_id"] == conversation or payload.get("memory_id") in moved
                   or payload.get("source_ref") in experience_refs or payload.get("node_id") in node_ids)
        if not belongs:
            continue
        if payload.get("node_id") in node_map:
            payload["node_id"] = node_map[payload["node_id"]]
        if payload.get("memory_scope") == old_scope:
            payload["memory_scope"] = new_scope
        for field in ("candidate_versions", "association_plan", "source_versions", "target_digest"):
            payload.pop(field, None)
        conn.execute("DELETE FROM consolidation_snapshots WHERE job_id=?", (row["id"],))
        conn.execute("UPDATE jobs SET scope=?,payload=?,attempts=attempts+CASE WHEN status='running' THEN 1 ELSE 0 END, "
                     "status=CASE WHEN status='running' THEN 'pending' ELSE status END,lease_until=NULL WHERE id=?",
                     (new_scope, json.dumps(payload, ensure_ascii=False), row["id"]))

    roots = {f"message:{m['id']}" for m in messages} | {m["source_ref"] for m in messages if m["source_ref"]}
    roots.update(moved)
    roots.update(memories[mid]["source_ref"] for mid in moved if memories[mid]["source_ref"])
    for ref in roots:
        row = conn.execute("SELECT * FROM temporal_retractions WHERE scope=? AND source_ref=?", (old_scope, ref)).fetchone()
        if row:
            conn.execute("INSERT OR IGNORE INTO temporal_retractions VALUES(?,?,?,?)", (new_scope, ref, row["fact_key"], row["valid_from"]))
            conn.execute("DELETE FROM temporal_retractions WHERE scope=? AND source_ref=?", (old_scope, ref))
    service.provenance.migrate(old_scope, new_scope, sorted(message_ids), experience_refs)
    for node in affected_nodes:
        if node["source_type"] == "raw":
            current = [repo.get_message(mid) for mid in json.loads(node["message_ids"])]
            if all(m and m["scope"] == new_scope for m in current):
                graph.register_raw(current, source_ref=node["source_ref"])
    for mid in moved - uncertain:
        graph.register_memory(mid)
    repo.temporal.rebuild_fact_intervals((old_scope, new_scope))
    service.enqueue_due_summaries()
    service._maybe_enqueue_summary(conversation, new_scope)
