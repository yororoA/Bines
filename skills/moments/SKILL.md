---
name: moments
description: 查看、发布、评论或点赞 Moments 动态。
actions: get_moments, add_moment, comment_moment, get_comments, like_moment, like_comment, analyze_moment_images
---
用户要求查看、评论、点赞或分析动态时，先调用 `get_moments` 并使用返回的 `_id`；评论、点赞或发布会产生外部写入，只有用户明确提出时执行。分析动态图片前先取得对应动态。
