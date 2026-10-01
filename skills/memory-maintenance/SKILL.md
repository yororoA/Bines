---
name: memory-maintenance
description: 更新角色状态、动态记忆和长期记录。
actions: update_status
---
只根据明确发生的对话变化更新字段。地点、关系、物品、任务、NPC 状态和记忆亮点均只写入有证据的变化；relationship_delta 使用整数，未变化的字段省略。
