# Bines 表情收藏

chatbot 与 full 共用 `napcat_collect_sticker`、`napcat_search_stickers`、`napcat_send_sticker`。Bines 可以自主选择收到的表情收藏，按对话含义检索后使用；不批量收藏所有照片，不要求每条回复带图。

- 来源：当前 QQ 消息、实际引用、同会话已保存且未撤回的历史消息；主人私聊或本机还可按真实 MD5 导入 QQ 账号已有收藏。自主后台收藏历史图必须指定 message_id。
- 格式：PNG、JPEG、WebP、GIF（保留原文件和动画），QQ `face`、商城 `mface`。最多 12 MiB，单帧 2000 万像素、500 帧、累计 1 亿解码像素；损坏或超限明确拒绝。
- 存储：网关持久卷 `gateway.db` 的 `sticker_library` 表，以及 `deploy/napcat/data/media/stickers/`。备份须同时保留两者。NapCat 读取对应的 `/bines-media/stickers/` 文件。
- 权限：默认原会话范围，主人私聊与 full 本机共用 owner 范围；主人在群内仍不能读取私聊表情库。`shared=true` 只允许本机或主人私聊显式指定公开复用，严格接受布尔值。
- 检索：描述与标签持久保存，中文子串/关键词排序，每次最多 5 个短候选。稳定 ID 不受收藏顺序影响；不会将全库、签名 URL、base64 或商城 key 注入提示词，不自动逐图调用视觉模型。
- QQ 同步：主人私聊的图片先保存本地，再尝试添加 QQ 收藏；群聊与其他用户只存本地。静默自主轮次也只存本地。内置/商城表情保存原生段，不声称已变成 QQ 图片收藏。

`local_saved=true` 仅表示本地保存成功；`qq_sync=confirmed` 才表示已经从 QQ 收藏列表按 MD5 确认。`pending/uncertain` 禁止换编号重新添加；`napcat_operation(operation_id)` 会只读核实迟到的成功并更新原回执。`local_only/not_applicable/failed` 不等于同步成功。收藏的 QQ 写入按内容 MD5 使用持久回执去重；发送仍使用现有目标权限、租约、冷却与回执链路。

隔离验证位于 `thingking/tests/test_sticker_*.py`，包括真实本机 HTTP 网关与模拟 NapCat、重启持久化、动画字节、原消息撤回、下载中取消、跨会话权限、未知写入、并发/重复调用及 10,000 项收藏不增加固定提示词。测试不会向真实 QQ 发送消息或添加测试收藏。真实 QQ 平台支持已通过运行时 schema 核验，实际写入仍以每次工具回执为准。
