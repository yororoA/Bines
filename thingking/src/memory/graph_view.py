"""A complete local graph view, including sources not yet backfilled.

This reads source records and valid graph relationships. It never schedules
models or creates association nodes to make a visualization look complete.
"""
from __future__ import annotations

from .evidence import _active


def independent_material(graph, source):
    """Admin visibility includes audit records; retrieval eligibility stays unchanged."""
    meta = dict(source.get("meta") or {})
    meta.pop("audit_only", None)
    if meta.get("legacy_type") == "raw_conversation":
        meta.pop("legacy_type")
    if not _active({**source, "meta": meta}, "memory"):
        return None, None
    node = {
        "id": "memory:" + source["id"], "scope": source["scope"], "source_type": source["kind"],
        "memory_id": source["id"], "message_ids": [], "superseded": 0,
        "family": sorted(graph._roots(source)),
    }
    material = graph.material(node)
    if material:
        return node, material
    if _active(source, "memory"):
        return None, None  # An eligible source with invalid dependencies must stay hidden.
    dependencies = set(node["family"])
    dependencies.update(f"message:{mid}" for mid in source["meta"].get("source_message_ids") or [])
    if any(not graph._message_valid(int(root.split(":", 1)[1]), source["scope"])
           for root in dependencies if root.startswith("message:")):
        return None, None
    return node, {"text": source["content"], "sources": [source]}


def graph_snapshot(service, scopes=None):
    repo, graph = service.repo, service.graph
    scope_set = set(scopes or ())
    nodes, originals, memories, messages = {}, {}, set(), set()

    def visible(scope):
        return not scope_set or scope in scope_set

    def add(node, material, registered, prior):
        sources = material["sources"]
        source = sources[0]
        node_id = node["id"]
        detail = ({"node_id": node_id} if registered else
                  {"memory_id": node["memory_id"]} if node["memory_id"] else
                  {"message_id": node["message_ids"][0]})
        nodes[node_id] = {
            "id": node_id, "kind": node["source_type"], "scope": node["scope"],
            "status": node.get("retrieval_status", "hot"), "ready": bool(node.get("ready")),
            "registered": registered, "preview": material["text"][:280],
            "search_text": material["text"],
            "record_only": node["source_type"] != "raw" and not _active(source, "memory"),
            "day_key": source.get("day_key"), "ts": source.get("ts") or source.get("end_ts") or source.get("created_at"),
            "memory_id": node["memory_id"], "message_ids": node["message_ids"],
            "max_weight": prior, "max_recall": prior, "max_citation": prior,
            "weight_basis": "prior" if registered else "initial", "chains": [],
            "detail_params": detail,
        }
        originals[node_id] = node
        if node["memory_id"]:
            memories.add(node["memory_id"])
        else:
            messages.update(node["message_ids"])

    for row in repo.conn.execute("SELECT id FROM association_nodes WHERE superseded=0 ORDER BY seq"):
        node = graph.get(row["id"])
        if not visible(node["scope"]) or not graph.valid(node):
            continue
        add(node, graph.material(node), True, service.usage.prior(node))

    for batch in repo.all_active_memories():
        for source in batch:
            if source["id"] in memories or not visible(source["scope"]):
                continue
            node, material = independent_material(graph, source)
            if material:
                prior = .2 + min(10, max(0, source["importance"])) * .06
                add(node, material, False, prior)

    for row in repo.conn.execute("SELECT * FROM messages ORDER BY id"):
        source = dict(row)
        if source["id"] in messages or not visible(source["scope"]) or not _active(source, "message", include_archived=True):
            continue
        node = {
            "id": f"message:{source['id']}", "scope": source["scope"], "source_type": "raw",
            "memory_id": None, "message_ids": [source["id"]],
            "retrieval_status": "cold" if source.get("episode_id") or source["origin"] == "archived" else "hot",
        }
        add(node, {"text": source["content"], "sources": [source]}, False, .5)

    edges, chains = [], []
    for row in repo.conn.execute("SELECT * FROM association_chains ORDER BY id").fetchall():
        chain = dict(row)
        if not visible(chain["scope"]):
            continue
        active_edges, member_ids = [], set()
        for edge in repo.conn.execute("SELECT * FROM association_edges WHERE chain_id=? ORDER BY id", (chain["id"],)):
            source, target = originals.get(edge["source_node"]), originals.get(edge["target_node"])
            valid_versions = (source and target and source.get("revision") == edge["source_revision"]
                              and target.get("revision") == edge["target_revision"])
            same_scope = source and target and source["scope"] == target["scope"] == chain["scope"]
            if not valid_versions or not same_scope or edge["confidence"] <= 0:
                continue
            active_edges.append({
                "id": edge["id"], "source": source["id"], "target": target["id"], "chain_id": chain["id"],
                "relation": edge["relation_type"], "confidence": edge["confidence"], "direction": edge["direction"],
            })
            member_ids.update((source["id"], target["id"]))
        if not active_edges:
            continue
        weights = graph.weights(chain["id"], chain["scope"])
        chain_weights = {}
        for node_id in sorted(member_ids):
            member = weights.get(node_id)
            if not member:
                continue
            node = nodes[node_id]
            recall, citation = member["recall_strength"], member["citation_strength"]
            if not node["chains"]:
                node["max_weight"] = node["max_recall"] = node["max_citation"] = 0
            node["max_recall"] = max(node["max_recall"], recall)
            node["max_citation"] = max(node["max_citation"], citation)
            node["max_weight"] = max(node["max_weight"], recall, citation)
            node["weight_basis"] = "chain_max"
            node["chains"].append(chain["id"])
            chain_weights[node_id] = {"recall": recall, "citation": citation}
        edges.extend(active_edges)
        chains.append({
            "id": chain["id"], "scope": chain["scope"], "reason": chain["reason_text"],
            "reason_key": chain["reason_key"], "members": sorted(member_ids), "weights": chain_weights,
        })
    result = list(nodes.values())
    return {
        "nodes": result, "edges": edges, "chains": chains,
        "scopes": sorted({n["scope"] for n in result}),
        "counts": {
            "nodes": len(result), "edges": len(edges), "chains": len(chains),
            "raw": sum(n["kind"] == "raw" for n in result),
            "memories": sum(n["kind"] != "raw" for n in result),
            "unregistered": sum(not n["registered"] for n in result),
        },
        "brightness": "max(recall,citation) across valid reason chains; prior for independent nodes",
        "waterline": graph.waterline(), "generated_at": service.clock(),
    }
