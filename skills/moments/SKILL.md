---
name: moments
description: 查看、发布、评论或点赞 Moments 动态。
actions: get_moments, add_moment, comment_moment, get_comments, like_moment, like_comment, analyze_moment_images
---
用户要求查看、评论、点赞或分析动态时，先调用 `get_moments` 并使用返回的 `_id`；评论、点赞或发布会产生外部写入，只有用户明确提出时执行。分析动态图片前先取得对应动态。

- `get_moments(page=1, limit=20)` 只读取已发布内容，最多每页 50 条；依据 `pagination.has_more / next_page` 翻页。正文换行保留。
- `get_comments(comment_ids=[...])` 最多 50 个 ID；MCP 也支持 `get_comments(moment_id="...", page=1, limit=20)`。两种选择不可同时传入，动态 `comments_has_more=true` 时必须按动态分页。
- MCP 身份由服务端绑定，不要在参数中伪造 UID。
- `add_moment`、`comment_moment`、`like_moment`、`like_comment` 均接受 `request_id`。为每个新写入提供唯一编号（UUID 或 8–128 位字母数字及 `._:-`）；超时后保留原编号和完全相同的参数。
- 结果未确认时，使用 ark-community 技能的 `get_ark_operation(request_id)` 查询。`completed` 表示已完成；`processing/unknown` 不代表失败，不得换编号重发。legacy 写入失败需在网页核查，禁止盲目重试。
- 配置或权限错误应直接报告；不要改调原始 REST 或改用另一个账号绕过。
