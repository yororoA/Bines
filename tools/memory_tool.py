"""Update scoped dynamic state through the Memory Server, with optimistic concurrency."""
import copy

from .dependencies import deps

OWNER_SCOPE = "owner"
_MAX_CONFLICT_RETRIES = 3


def _memory_client():
    from tools.context import CURRENT_CONTEXT
    identity = CURRENT_CONTEXT.get().identity
    if identity.local:
        client = deps.memory_client
    else:
        from chatBot.memory_hub import GatewayMemoryClient
        client = GatewayMemoryClient()
    if client is None:
        raise RuntimeError("记忆服务未就绪")
    return client, identity


def _snippet(text, query, counter, units):
    from memory.evidence import _short_evidence
    # Recenter after reducing the character window: cutting the prefix of a
    # Chinese match-centered excerpt can otherwise discard the actual match.
    low, high, best = 1, min(700, len(text)), ""
    while low <= high:
        size = (low + high) // 2
        span = _short_evidence(text, query, size)
        if counter.count(span) <= units:
            best, low = span, size + 1
        else:
            high = size - 1
    return best


def search_memory(query: str, k: int = 8):
    """检索有界主题片段；通过返回的citation_id调用read_memory_source分页展开。"""
    import json
    from common.context_budget import ContextTokenCounter, context_limit, diagnostic
    from tools.context import CURRENT_CONTEXT
    client, identity = _memory_client()
    k = max(1, min(int(k), 20))
    mode = "explore" if CURRENT_CONTEXT.get().autonomous else "auto"
    reply = client.search_with_trace(query, [identity.scope], k=k, retrieval_mode=mode)
    hits, trace = reply["hits"], reply.get("trace") or {}
    counter = ContextTokenCounter()
    budget = max(128, min(8192, context_limit() // 4))
    result, shown = [], []
    for hit in hits:
        matched = [span for span in (hit.get("matched_spans") or [])
                   if isinstance(span, str) and span and span in hit["content"]]
        # Vector/alias retrieval can locate a passage without any literal query
        # term in the original. Keep that located span instead of searching the
        # whole source again and accidentally returning its unrelated opening.
        span = _snippet(matched[0] if matched else hit["content"], query, counter, 640)
        evidence = [{key: ev.get(key) for key in
                     ("source_type", "source_ref", "role", "ts", "action", "status")}
                    | {"content": _snippet(ev.get("content") or "", query, counter, 256)}
                    for ev in (hit.get("evidence") or [])[:3]]
        associations = [{"id": row["id"], "reason": counter.prefix(str(row.get("reason") or ""), 160)}
                        for row in (hit.get("associations") or [])[:2]]
        candidate = {"id": hit["id"], "citation_id": hit.get("citation_id"),
                     "chain_ids": [row["id"] for row in associations], "associations": associations,
                     "content": span, "kind": hit["kind"], "day_key": hit.get("day_key"),
                     "evidence": evidence, "corrections": hit.get("corrections") or [],
                     "is_excerpt": span != hit["content"],
                     "expand": {"source_id": hit.get("citation_id"), "offset": max(0, hit["content"].find(span))}}
        if hit.get("retrieval_role") == "exploration":
            candidate["retrieval_role"] = "exploration"
            candidate["notice"] = "探索线索，须核验；不能当作当前问题的已确认事实"
        # Corrections are never silently dropped to make old facts appear valid.
        if counter.count(json.dumps([*result, candidate], ensure_ascii=False)) > budget:
            continue
        result.append(candidate)
        shown.append((hit, candidate))
    journal = CURRENT_CONTEXT.get().journal
    if journal:
        for hit, candidate in shown:
            journal.present_memory({**hit, "chain_ids": candidate["chain_ids"]},
                                   [candidate["content"], *[e["content"] for e in candidate["evidence"] if e["content"]]])
    diagnostic("tool", {"action": "search_memory", "retrieval": trace, "candidates": len(hits),
                        "shown": len(result), "omitted": len(hits) - len(result), "limit": budget,
                        "input_units": counter.count(json.dumps(result, ensure_ascii=False)),
                        "counting_method": counter.method})
    if hits and not result:
        raise ValueError("检索命中资料，但完整来源与纠正说明超过本轮工具预算；请缩小查询范围")
    return result


def read_memory_source(source_id: str, offset: int = 0, limit: int = 1024):
    """按字符偏移展开当前会话来源；页大小另受输入预算约束，next_offset用于下一页。"""
    import json
    from common.context_budget import ContextTokenCounter, context_limit, diagnostic
    from tools.context import CURRENT_CONTEXT
    client, identity = _memory_client()
    page = client.read_memory_source(source_id, identity.scope, offset=offset, limit=limit)
    if page is None:
        return {"status": "unavailable", "source_id": source_id,
                "message": "来源不存在、已失效或不属于当前会话"}
    page = dict(page)
    counter = ContextTokenCounter()
    budget = max(128, min(4096, context_limit() // 4))
    text = page.pop("content")
    overhead = counter.count(json.dumps({**page, "content": ""}, ensure_ascii=False)) + 32
    if overhead >= budget:
        raise ValueError("来源元数据与纠正说明超过本轮预算，未展示旧原文")
    shown = counter.prefix(text, budget - overhead)
    page["content"] = shown
    if len(shown) < len(text):
        page["next_offset"] = page["offset"] + len(shown)
        page["is_excerpt"] = True
    journal = CURRENT_CONTEXT.get().journal
    if journal and shown:
        journal.present_memory(page, [shown])
    diagnostic("tool", {"action": "read_memory_source", "source_id": source_id,
                        "offset": page["offset"], "next_offset": page["next_offset"],
                        "input_units": counter.count(json.dumps(page, ensure_ascii=False)),
                        "limit": budget, "counting_method": counter.method})
    return page


def cite_memory(source_id: str, answer_span: str, source_quote: str = "", chain_id: str = ""):
    """标注本轮确实使用的记忆来源；answer_span 必须出现在最终回答中，原话引用附 source_quote。"""
    from tools.context import CURRENT_CONTEXT
    journal = CURRENT_CONTEXT.get().journal
    if not journal:
        return "本轮没有可记录的记忆引用上下文。"
    journal.cite_memory(source_id, answer_span, source_quote, chain_id)
    return "引用回执已暂存，轮次结束后核对来源与实际回答。"


def correct_memory(source_id: str, quote: str, replacement: str, explanation: str, correction_quote: str):
    """按当前用户原话纠正已呈现的错误事实，完成本轮后核验并重评关联。"""
    from tools.context import CURRENT_CONTEXT
    journal = CURRENT_CONTEXT.get().journal
    if not journal:
        raise RuntimeError("本轮没有可核验的记忆纠错上下文")
    journal.correct_memory(source_id, quote, replacement, explanation, correction_quote)
    return "事实纠正已暂存，完成本轮后按真实用户原话核验并重评关联。"


def _apply_changes(state, *, current_location=None, relationship_delta=None, add_item=None, remove_item=None,
                   active_quest=None, current_time=None, npc_name=None, npc_attire=None, npc_visual_status=None,
                   npc_activity=None, add_memory_highlight=None, remove_memory_highlight=None, important_thing=None):
    state = copy.deepcopy(state)
    for key, value in (("current_time", current_time), ("current_location", current_location),
                       ("active_quest", active_quest), ("important_thing", important_thing)):
        if value is not None:
            state[key] = value
    if relationship_delta is not None:
        from relationship_state import apply_relationship_delta
        score, level, weights = apply_relationship_delta(
            current_score=int(state.get("relationship_score", 0) or 0), delta=relationship_delta,
        )
        state.update(relationship_score=score, relationship_level=level, relationship_distribution=weights)
    inventory = list(state.get("inventory") or [])
    if add_item and add_item not in inventory:
        inventory.append(add_item)
    if remove_item and remove_item in inventory:
        inventory.remove(remove_item)
    state["inventory"] = inventory
    npc = dict(state.get("npc_state") or {})
    for key, value in (("name", npc_name), ("attire", npc_attire),
                       ("visual_module_status", npc_visual_status), ("current_activity", npc_activity)):
        if value is not None:
            npc[key] = value
    state["npc_state"] = npc
    highlights = list(state.get("memory_highlights") or [])
    if add_memory_highlight and add_memory_highlight not in highlights:
        highlights.append(add_memory_highlight)
    if remove_memory_highlight and remove_memory_highlight in highlights:
        highlights.remove(remove_memory_highlight)
    state["memory_highlights"] = highlights
    return state


def update_status(current_location=None, relationship_level=None, relationship_delta: int | None = None,
                  add_item: str | None = None, remove_item: str | None = None, active_quest: str | None = None,
                  current_time: str | None = None, npc_name: str | None = None, npc_attire: str | None = None,
                  npc_visual_status: str | None = None, npc_activity: str | None = None,
                  add_memory_highlight: str | None = None, remove_memory_highlight: str | None = None,
                  important_thing: str | None = None, expected_version: int | None = None,
                  expected_state: dict | None = None):
    """relationship_level is retained for old callers; use relationship_delta for changes."""
    from tools.context import CURRENT_CONTEXT
    identity = CURRENT_CONTEXT.get().identity
    if identity.local:
        client = deps.memory_client
        scope = OWNER_SCOPE
    else:
        from chatBot.memory_hub import GatewayMemoryClient
        client = GatewayMemoryClient()
        scope = identity.state_scope
    if client is None:
        return "Dynamic memory is not available."
    try:
        delta = int(relationship_delta) if relationship_delta is not None else None
    except (TypeError, ValueError):
        return f"状态更新失败：relationship_delta 必须是整数（收到 {relationship_delta!r}）。"
    last_error = ""
    context = CURRENT_CONTEXT.get()
    if context.journal and context.identity.local:
        source_ref = context.journal.prefix + ":user" if not context.autonomous else context.journal.prefix + ":turn"
    else:
        source_ref = None
    for _ in range(_MAX_CONFLICT_RETRIES):
        current = client.get_state(scope)
        if expected_version is not None and expected_version != current.get("version"):
            return "状态更新失败：状态已被其他线程更新，请重新读取后再操作"
        updated = _apply_changes(
            current.get("data") or {}, current_location=current_location, relationship_delta=delta,
            add_item=add_item, remove_item=remove_item, active_quest=active_quest, current_time=current_time,
            npc_name=npc_name, npc_attire=npc_attire, npc_visual_status=npc_visual_status, npc_activity=npc_activity,
            add_memory_highlight=add_memory_highlight, remove_memory_highlight=remove_memory_highlight,
            important_thing=important_thing,
        )
        try:
            result = client.put_state(scope, updated, expected_version=current.get("version"),
                                      **({"source_ref": source_ref} if source_ref else {}))
        except RuntimeError as exc:
            if "StateConflict" not in str(exc):
                raise
            last_error = str(exc)
            continue
        warnings = [f"警告：{key} 与预期状态不符" for key, value in (expected_state or {}).items()
                    if key in updated and updated[key] != value]
        return (f"动态状态已更新 (版本: {current.get('version')} -> {result['version']})"
                + ("\n" + "\n".join(warnings) if warnings else ""))
    return f"状态更新失败：状态被并发修改，重试 {_MAX_CONFLICT_RETRIES} 次仍冲突（{last_error}）。"
