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


def _search_view(hit, query, turn_text, counter):
    from common.memory_presentation import (
        diary_witnesses, grouped_message_offset, hide_turn_prose, history_wording, narrative_original_spans,
        narrative_spans, supported_evidence, wants_narrative_original, wants_turn_record, wants_verbatim,
    )
    source = hit["content"]
    kind = hit["kind"]
    quoted = wants_verbatim({"current_query": turn_text}) or bool((hit.get("meta") or {}).get("curated_quote"))
    original = wants_narrative_original(turn_text, kind)
    rows = (supported_evidence(hit, turn_text) if kind in {"episode", "diary", "semantic"}
            else hit.get("evidence") or [])
    # Vector/alias retrieval can locate a passage with no literal query match.
    if hide_turn_prose(hit, turn_text):
        selected = []
    elif kind in {"episode", "diary"}:
        selected = narrative_original_spans(hit, query, 700) if original else narrative_spans(hit, query)
        if original:
            rows = []
        elif kind == "diary" and not quoted and not wants_turn_record(turn_text):
            rows = diary_witnesses(hit)
    else:
        selected = [next((s for s in hit.get("matched_spans") or [] if isinstance(s, str) and s and s in source), source)]
    spans = [_snippet(s, query, counter, 640) for s in selected]
    span = spans[0] if spans else ""
    shown = " … ".join(spans)
    offset = hit.get("citation_offset", 0) + source.find(span)
    evidence = []
    for row in rows[:3]:
        text = row.get("content") or ""
        excerpt = _snippet(text, query, counter, 256)
        display, literals = excerpt, [excerpt]
        assistant = row.get("source_type") == "message" and row.get("role") == "assistant"
        if assistant:
            display, literals = history_wording(excerpt, verbatim=quoted)
        if kind == "message" and not evidence:
            speaker_offset = grouped_message_offset(hit, row)
            if speaker_offset is not None:
                # A grouped transcript can include unselected speakers and old
                # replies. Show the ranked speaker, with its exact page offset.
                offset = speaker_offset + text.index(excerpt)
                shown, spans = display, []
            elif assistant:
                shown, spans = history_wording(span, verbatim=quoted)
        evidence.append({key: row.get(key) for key in (
            "source_type", "source_id", "source_ref", "role", "actor_id", "actor_name", "ts", "action", "status",
        )} | {"content": display})
        spans.extend(literals)
    return shown, evidence, offset, [s for s in dict.fromkeys(spans) if s]


def search_memory(query: str, k: int = 8):
    """检索有界主题片段；通过返回的citation_id调用read_memory_source分页展开。"""
    import json
    from common.context_budget import ContextTokenCounter, context_limit, diagnostic
    from tools.context import CURRENT_CONTEXT
    client, identity = _memory_client()
    k = max(1, min(int(k), 20))
    context = CURRENT_CONTEXT.get()
    mode = "explore" if context.autonomous else "auto"
    turn_text = "" if context.autonomous else context.user_text or query
    reply = client.search_with_trace(query, [identity.scope], k=k, retrieval_mode=mode)
    hits, trace = reply["hits"], reply.get("trace") or {}
    counter = ContextTokenCounter()
    budget = max(128, min(8192, context_limit() // 4))
    result, shown = [], []
    for hit in hits:
        span, evidence, offset, source_spans = _search_view(hit, query, turn_text, counter)
        associations = [{"id": row["id"], "reason": counter.prefix(str(row.get("reason") or ""), 160)}
                        for row in (hit.get("associations") or [])[:2]]
        candidate = {"id": hit["id"], "citation_id": hit.get("citation_id"),
                     "chain_ids": [row["id"] for row in associations], "associations": associations,
                     "content": span, "kind": hit["kind"], "day_key": hit.get("day_key"),
                     "evidence": evidence, "corrections": hit.get("corrections") or [],
                     "is_excerpt": span != hit["content"],
                     "expand": {"source_id": hit.get("citation_id"), "offset": offset}}
        if hit.get("temporal"):
            candidate["temporal"] = hit["temporal"]
        meta = hit.get("meta") or {}
        for field in ("action", "status"):
            if value := hit.get(field) or meta.get(field):
                candidate[field] = value
        if hit.get("retrieval_role") == "exploration":
            candidate["retrieval_role"] = "exploration"
            candidate["notice"] = "探索线索，须核验；不能当作当前问题的已确认事实"
        # Corrections are never silently dropped to make old facts appear valid.
        if counter.count(json.dumps([*result, candidate], ensure_ascii=False)) > budget:
            continue
        result.append(candidate)
        shown.append(({**hit, "chain_ids": candidate["chain_ids"]}, source_spans))
    if context.journal:
        for hit, source_spans in shown:
            context.journal.present_memory(hit, source_spans)
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


def learn_expression(technique: str, situation: str, source_quote: str, source_id: str = ""):
    """学习真实网页/字幕中的少女语气。technique 取 short_reaction/softener/echo_play/playful_exaggeration/self_mock/curious_followup/natural_particles/light_teasing/tease_then_care/warm_direct/understated_pride/playful_reframe/gentle_protest/varied_rhythm/leave_space/specific_response；situation 取 casual/praised/teased/upset/celebration/parting。quote 必须逐字来自原文，只学表达方式，不复制身份或整段台词。"""
    from tools.context import CURRENT_CONTEXT
    context = CURRENT_CONTEXT.get()
    if context.planning_only:
        raise PermissionError("休息思考轮次不新增表达经验")
    reason = context.check() if context.check else ""
    if reason:
        raise RuntimeError(f"表达学习已取消：{reason}")
    journal = context.journal
    if journal is None:
        raise RuntimeError("本轮没有可核验的表达来源")
    source = journal.resolve_expression_source(source_quote, source_id)
    client, identity = _memory_client()
    # QQ's hub owns outbox delivery. Desktop has the trusted direct client;
    # do not send record_experience through an ordinary QQ identity.
    if identity.local and source.get("kind") == "experience":
        journal.outbox.sync(client.call, limit=50, time_budget=1.0)
    reason = context.check() if context.check else ""
    if reason:
        raise RuntimeError(f"表达学习已取消：{reason}")
    return client.learn_expression(identity.scope, source, source_quote, technique, situation)


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
    previous = state.setdefault("memory_highlights", [])
    if add_memory_highlight or remove_memory_highlight:
        # Old imports can contain one string or a custom JSON value. Keep a
        # single entry intact and never rewrite it merely to update the clock.
        highlights = previous if isinstance(previous, list) else ([] if previous is None else [previous])
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
    context = CURRENT_CONTEXT.get()
    identity = context.identity
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
    if context.journal and context.identity.local:
        source_ref = context.journal.prefix + ":user" if not context.autonomous else context.journal.prefix + ":turn"
    else:
        source_ref = None
    changes = {key: value for key, value in {
        "current_location": current_location, "relationship_delta": delta,
        "add_item": add_item, "remove_item": remove_item, "active_quest": active_quest,
        "current_time": current_time, "npc_name": npc_name, "npc_attire": npc_attire,
        "npc_visual_status": npc_visual_status, "npc_activity": npc_activity,
        "add_memory_highlight": add_memory_highlight, "remove_memory_highlight": remove_memory_highlight,
        "important_thing": important_thing,
    }.items() if value is not None}
    notice = ""
    if context.autonomous and (add_memory_highlight is not None or remove_memory_highlight is not None):
        # The enclosing tool receipt retains the complete note as Bines' own
        # experience. Only dialogue or an administrator edits shared highlights.
        changes.pop("add_memory_highlight", None)
        changes.pop("remove_memory_highlight", None)
        notice = "自主轮次的笔记随工具经历记录，共同高光保持原样。"
        if not changes:
            return notice if context.journal else "自主轮次未变更共同高光，本轮没有经历记录。"
    provenance = {"source_ref": source_ref} if source_ref else {}
    if context.autonomous and identity.local:
        from memory.channels import local_envelope
        provenance["autonomous_envelope"] = local_envelope().to_dict()
    for _ in range(_MAX_CONFLICT_RETRIES):
        current = client.get_state(scope)
        if expected_version is not None and expected_version != current.get("version"):
            return "状态更新失败：状态已被其他线程更新，请重新读取后再操作"
        updated = _apply_changes(current.get("data") or {}, **changes)
        try:
            result = client.put_state(scope, updated, expected_version=current.get("version"), changes=changes,
                                      **provenance)
        except RuntimeError as exc:
            if "StateConflict" not in str(exc):
                raise
            last_error = str(exc)
            continue
        committed = result.get("data")
        if not isinstance(committed, dict):
            committed = updated  # Older clients may return only the version.
        warnings = [f"警告：{key} 与预期状态不符" for key, value in (expected_state or {}).items()
                    if key in committed and committed[key] != value]
        return (f"动态状态已更新 (版本: {current.get('version')} -> {result['version']})"
                + ("\n" + notice if notice else "")
                + ("\n" + "\n".join(warnings) if warnings else ""))
    return f"状态更新失败：状态被并发修改，重试 {_MAX_CONFLICT_RETRIES} 次仍冲突（{last_error}）。"
