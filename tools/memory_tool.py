"""Update scoped dynamic state through the Memory Server, with optimistic concurrency."""
import copy

from .dependencies import deps

OWNER_SCOPE = "owner"
_MAX_CONFLICT_RETRIES = 3


def _apply_changes(state, *, current_location, relationship_delta, add_item, remove_item, active_quest,
                   current_time, npc_name, npc_attire, npc_visual_status, npc_activity, add_memory_highlight,
                   remove_memory_highlight, important_thing):
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
            result = client.put_state(scope, updated, expected_version=current.get("version"))
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
