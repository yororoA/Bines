---
name: memory-maintenance
description: 更新角色状态、动态记忆和长期记录。
actions: update_status, search_memory
---
只根据明确发生的对话变化更新字段。地点、关系、物品、任务、NPC 状态和记忆亮点均只写入有证据的变化；relationship_delta 使用整数，未变化的字段省略。

用户询问以前的人、物品、歌曲或约定，而现有上下文不够时，调用 search_memory(query="主题关键词") 后回答，不要直接断言忘了。当前状态（好感度、物品、约定）和用户画像已在提示词中，优先读取；检索仅返回当前会话有权限查看的记忆。
