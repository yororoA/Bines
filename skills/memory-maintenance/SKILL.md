---
name: memory-maintenance
description: 更新角色状态、动态记忆和长期记录。
actions: update_status, search_memory, cite_memory, correct_memory
---
只根据明确发生的对话变化更新字段。地点、关系、物品、任务、NPC 状态和记忆亮点均只写入有证据的变化；relationship_delta 使用整数，未变化的字段省略。

用户询问以前的人、物品、歌曲或约定，而现有上下文不够时，调用 search_memory(query="主题关键词") 后回答，不要直接断言忘了。结果包含原始 evidence，优先依据 evidence 的原文和日期回答；只有 legacy_memory/summary 时才说明是摘要或旧记录，不能把推断当用户原话。当前状态（好感度、物品、约定）和用户画像已在提示词中，优先读取；检索仅返回当前会话有权限查看的记忆。

确实使用某条记忆回答时，可调用 cite_memory(source_id="返回的 citation_id", answer_span="最终回答中的连续片段") 记录来源；逐字引用还提供 source_quote="本轮可见的原文片段"。回执在轮次结束后核对，不给未使用资料、候选搜索或摘要任务记引用。来源 ID 只用于回执，无需展示给用户。

资料包含多个关联理由时，根据本次实际回答选择其中一个理由，以 chain_id="对应理由的 id" 提交引用。只有一个已呈现理由时服务会自动归属；无法确定理由时可以省略，回执保留但不同时强化所有关联链。不能填写本轮资料之外的关联理由。

引用时尽量提供支持回答的 source_quote；只有经核验的证据片段与所选理由才会学习当前问题中的理由份额。展示、建联和同事件的重复回执不构成独立支持。

本机或主人私聊中，用户明确纠正已呈现记忆时可用 correct_memory(source_id="citation_id", quote="可见旧事实", replacement="当前用户原话中的新事实", explanation="纠正说明", correction_quote="当前用户的连续原话")。纠错完成本轮后核验，保留原始消息并重新评估关联。自主轮次、普通私聊和群聊不能修改主人记忆。资料中的 corrections 表示旧事实已失效，应采用最新纠正。
