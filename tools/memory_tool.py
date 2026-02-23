"""
记忆状态更新工具
用于更新动态状态记忆：地点、好感度、物品、任务、用户状态、NPC状态、记忆亮点等
"""
import copy
from .dependencies import deps

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
                  important_thing: str | None = None,
                  expected_version: int | None = None,
                  expected_state: dict | None = None):
    """
    更新动态状态记忆：地点、好感度、物品、任务、用户状态、NPC状态、记忆亮点等。
    该函数会被思考模型或主模型通过工具调用触发。
    
    [并发安全说明]
    - 仅在两处短暂持锁：① 复制出当前 state ② 写回并 save()，中间在锁外计算，避免主线程读动态记忆（to_prompt_str）时长时间阻塞
    - 使用 RLock 以便 save() 在持锁逻辑内可被调用
    
    [并发覆盖风险与检测]
    - 在高并发场景下（如 Thinking 线程和 screen_monitor_thread 同时调用），可能出现逻辑覆盖
    - 例如：Thinking 模型基于"旧状态"决定删除物品，但在此期间物品状态已被其他事件改变
    - 当前实现：使用锁保证原子性，但无法检测"状态已变更"的情况
    - [新增] 使用 expected_state 参数检测状态差异，如果差异较大则返回警告信息
    
    Args:
        expected_version: 可选，期望的状态版本号（用于乐观锁，当前未启用）
        expected_state: 可选，期望的状态字典，用于检测状态差异
                       格式：{"inventory": ["A", "B"], "current_location": "地点", ...}
                       如果操作后的状态与预期差异较大，会在返回值中添加警告信息
    """
    # [依赖注入] 从依赖容器获取记忆系统
    if deps.memory_system is None:
        return "Dynamic memory is not available."
    
    try:
        dm = deps.memory_system.dynamic
    except AttributeError:
        return "Dynamic memory is not available."

    # ① 短暂持锁：复制出当前 state，避免主线程读动态记忆时长时间阻塞
    with dm.lock:
        if expected_version is not None:
            current_version = dm.state.get("_version", 0)
            if current_version != expected_version:
                return f"状态更新失败：状态版本不匹配（期望: {expected_version}, 实际: {current_version}）。状态可能已被其他操作修改。"
        operation_start_version = dm.state.get("_version", 0)
        s = copy.deepcopy(dm.state)

    # ② 锁外计算，主线程可在此期间读取 to_prompt_str()
    if current_time is not None:
        s["current_time"] = current_time
    if current_location is not None:
        s["current_location"] = current_location
    if active_quest is not None:
        s["active_quest"] = active_quest
    # 关系/好感度仅通过 relationship_delta 更新；relationship_level/score/distribution 由 apply_relationship_delta 计算
    if relationship_delta is not None:
        try:
            from relationship_state import apply_relationship_delta
            delta_int = int(relationship_delta) if not isinstance(relationship_delta, int) else relationship_delta
            current_score = int(s.get("relationship_score", 0) or 0)
            new_score, new_level, weights = apply_relationship_delta(
                current_score=current_score,
                delta=delta_int,
            )
            s["relationship_score"] = new_score
            s["relationship_level"] = new_level
            s["relationship_distribution"] = weights
        except Exception as e:
            print(f"[UpdateStatus] relationship_delta 处理失败 (delta={relationship_delta}): {e}", flush=True)

    inventory = list(s.get("inventory", [])) if isinstance(s.get("inventory"), list) else []
    if add_item and add_item not in inventory:
        inventory.append(add_item)
    if remove_item and remove_item in inventory:
        inventory.remove(remove_item)
    s["inventory"] = inventory

    if "npc_state" not in s or not isinstance(s["npc_state"], dict):
        s["npc_state"] = {"name": "", "attire": "", "visual_module_status": "", "current_activity": ""}
    if npc_name is not None:
        s["npc_state"]["name"] = npc_name
    if npc_attire is not None:
        s["npc_state"]["attire"] = npc_attire
    if npc_visual_status is not None:
        s["npc_state"]["visual_module_status"] = npc_visual_status
    if npc_activity is not None:
        s["npc_state"]["current_activity"] = npc_activity
    if important_thing is not None:
        s["important_thing"] = important_thing

    memory_highlights = list(s.get("memory_highlights", [])) if isinstance(s.get("memory_highlights"), list) else []
    if add_memory_highlight and add_memory_highlight not in memory_highlights:
        memory_highlights.append(add_memory_highlight)
    if remove_memory_highlight and remove_memory_highlight in memory_highlights:
        memory_highlights.remove(remove_memory_highlight)
    s["memory_highlights"] = memory_highlights

    # ③ 短暂持锁：写回并 save()
    with dm.lock:
        for k, v in s.items():
            dm.state[k] = copy.deepcopy(v)
        dm.save()

        warnings = []
        if expected_state is not None:
            # 检查关键字段的状态差异
            actual_state = {
                "inventory": dm.state.get("inventory", []),
                "current_location": dm.state.get("current_location", ""),
                "active_quest": dm.state.get("active_quest", ""),
                "relationship_score": dm.state.get("relationship_score", 0),
            }
            
            # 比较预期状态和实际状态
            for key, expected_value in expected_state.items():
                if key not in actual_state:
                    continue
                
                actual_value = actual_state[key]
                
                # 对于列表类型（如 inventory），检查是否有意外的增减
                if isinstance(expected_value, list) and isinstance(actual_value, list):
                    expected_set = set(expected_value)
                    actual_set = set(actual_value)
                    
                    # 检查是否有意外的物品丢失或增加
                    missing_items = expected_set - actual_set
                    unexpected_items = actual_set - expected_set
                    
                    if missing_items:
                        warnings.append(f"警告：{key} 中缺少预期物品: {list(missing_items)}。状态可能已被其他操作修改。")
                    if unexpected_items:
                        warnings.append(f"警告：{key} 中有意外物品: {list(unexpected_items)}。状态可能已被其他操作修改。")
                
                # 对于字符串类型（如 current_location），检查是否一致
                elif isinstance(expected_value, str) and isinstance(actual_value, str):
                    if expected_value and actual_value != expected_value:
                        warnings.append(f"警告：{key} 与预期不符（预期: {expected_value}, 实际: {actual_value}）。状态可能已被其他操作修改。")
                
                # 对于数值类型（如 relationship_score），检查差异是否过大
                elif isinstance(expected_value, (int, float)) and isinstance(actual_value, (int, float)):
                    diff = abs(actual_value - expected_value)
                    if diff > 5:  # 如果差异超过5，认为差异较大
                        warnings.append(f"警告：{key} 与预期差异较大（预期: {expected_value}, 实际: {actual_value}, 差异: {diff}）。状态可能已被其他操作修改。")
        
        # 构建返回结果
        result_msg = f"动态状态已更新"
        if expected_version is not None:
            result_msg += f" (版本: {operation_start_version} -> {dm.state.get('_version', operation_start_version)})"
        
        # 如果有警告，添加到返回结果中
        if warnings:
            result_msg += "\n" + "\n".join(warnings)
            print(f"[UpdateStatus] 状态差异警告: {len(warnings)} 条", flush=True)
            for warning in warnings:
                print(f"  - {warning}", flush=True)
        
        return result_msg
