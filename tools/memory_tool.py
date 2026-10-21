"""
记忆状态更新工具
更新管理员（owner）作用域的动态状态：地点、好感度、物品、任务、角色状态、记忆亮点等。
状态保存在 Memory Server 的 SQLite 中，通过版本号做乐观并发控制。
"""
import copy

from .dependencies import deps

OWNER_SCOPE = "owner"
_MAX_CONFLICT_RETRIES = 3


def _apply_changes(state, *, current_location, relationship_delta, add_item, remove_item, active_quest,
                   current_time, npc_name, npc_attire, npc_visual_status, npc_activity, add_memory_highlight,
                   remove_memory_highlight, important_thing):
    s = copy.deepcopy(state)
    if current_time is not None:
        s["current_time"] = current_time
    if current_location is not None:
        s["current_location"] = current_location
    if active_quest is not None:
        s["active_quest"] = active_quest
    if important_thing is not None:
        s["important_thing"] = important_thing
    # 关系/好感度只通过 relationship_delta 更新；等级与分布由 apply_relationship_delta 计算
    if relationship_delta is not None:
        from relationship_state import apply_relationship_delta

        new_score, new_level, weights = apply_relationship_delta(
            current_score=int(s.get("relationship_score", 0) or 0),
            delta=int(relationship_delta),
        )
        s["relationship_score"] = new_score
        s["relationship_level"] = new_level
        s["relationship_distribution"] = weights

    inventory = list(s.get("inventory") or [])
    if add_item and add_item not in inventory:
        inventory.append(add_item)
    if remove_item and remove_item in inventory:
        inventory.remove(remove_item)
    s["inventory"] = inventory

    npc = dict(s.get("npc_state") or {})
    for key, value in (("name", npc_name), ("attire", npc_attire),
                       ("visual_module_status", npc_visual_status), ("current_activity", npc_activity)):
        if value is not None:
            npc[key] = value
    s["npc_state"] = npc

    highlights = list(s.get("memory_highlights") or [])
    if add_memory_highlight and add_memory_highlight not in highlights:
        highlights.append(add_memory_highlight)
    if remove_memory_highlight and remove_memory_highlight in highlights:
        highlights.remove(remove_memory_highlight)
    s["memory_highlights"] = highlights
    return s


def update_status(current_location=None,
                  relationship_level=None,
                  relationship_delta: int | None = None,
                  add_item: str | None = None,
                  remove_item: str | None = None,
                  active_quest: str | None = None,
                  current_time: str | None = None,
                  npc_name: str | None = None,
                  npc_attire: str | None = None,
                  npc_visual_status: str | None = None,
                  npc_activity: str | None = None,
                  add_memory_highlight: str | None = None,
                  remove_memory_highlight: str | None = None,
                  important_thing: str | None = None):
    """
    更新动态状态记忆。由本地 SkillRunner 按文本协议分发调用。
    relationship_level 仅为兼容旧调用而保留，会被忽略；关系变化请使用 relationship_delta。
    """
    client = deps.memory_client
    if client is None:
        return "Dynamic memory is not available."
    try:
        delta = int(relationship_delta) if relationship_delta is not None else None
    except (TypeError, ValueError):
        return f"状态更新失败：relationship_delta 必须是整数（收到 {relationship_delta!r}）。"

    last_error = ""
    for _ in range(_MAX_CONFLICT_RETRIES):
        current = client.get_state(OWNER_SCOPE)
        updated = _apply_changes(
            current.get("data") or {},
            current_location=current_location, relationship_delta=delta, add_item=add_item,
            remove_item=remove_item, active_quest=active_quest, current_time=current_time, npc_name=npc_name,
            npc_attire=npc_attire, npc_visual_status=npc_visual_status, npc_activity=npc_activity,
            add_memory_highlight=add_memory_highlight, remove_memory_highlight=remove_memory_highlight,
            important_thing=important_thing,
        )
        try:
            result = client.put_state(OWNER_SCOPE, updated, expected_version=current.get("version"))
        except RuntimeError as exc:
            if "StateConflict" not in str(exc):
                raise
            last_error = str(exc)
            continue
        return f"动态状态已更新 (版本: {current.get('version')} -> {result['version']})"
    return f"状态更新失败：状态被并发修改，重试 {_MAX_CONFLICT_RETRIES} 次仍冲突（{last_error}）。"
