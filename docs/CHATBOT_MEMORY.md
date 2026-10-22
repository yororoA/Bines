# ChatBot 记忆模式

所有渠道使用新版 MemoryService。完整模式由独立的 RAG Server 持有 `memory.db` 和 Chroma 向量索引；纯 QQ 模式把服务嵌入网关，使用词面检索，不下载嵌入模型，仍只运行网关和 QQ Thinking 两个 Python 进程。数据目录由 `.env` 中的 `BINES_MEMORY_DIR` 指定，默认 `thingking/memory_data`。

QQ 原始消息、事件队列、回复轮次和工具发送回执放在独立的 `gateway.db`。网关确认记忆服务提交后才标记同步成功，重启时继续同步，通过来源标识去重。启动时会识别旧 NapCat `memory.db` 的表结构，先迁移为 `gateway.db`，再创建新版记忆库。

## 每轮回复

1. 从真实 OneBot 事件计算机器人账号、用户和群号；正文中的身份声明不改变权限。
2. 按当前会话补取最多 30 条 NapCat 历史，去重并同步到 MemoryService。
3. 读取最近 30 条会话记录、较早情节摘要、相关长期事实和记忆，以及当前个人状态。引用 ID 从原始消息附带。
4. 人格和场景提示词统一由 `persona.yaml` 与 `prompt_builder` 生成。QQ 输入、引用、图片观察和工具结果作为数据处理。
5. 回复确认发送成功后才记录 assistant；结果不确定时保留回执并暂停，禁止自动重复发送。

QQ Thinking 的后台 MemoryWorker 对所有渠道进行情节总结、稳定事实提取和主人日记整理。默认每积累 40 条待整合消息，总结较早的 30 条；任务和重试保存在 SQLite 中。完整模式使用向量召回，纯 QQ 使用词面召回，两种模式共享原始记录与派生记忆。

记忆任务沿用 `DEEPSEEK_THINKING` 的思考模式；推理与正文共享输出额度。摘要和日记默认给 8192 个 token，状态更新给 4096 个 token；摘要与状态更新使用 JSON 输出格式。遇到 `finish_reason=length` 时，同一请求以 262144 个 token 的 256k 上限重试一次。仍被截断或正文为空时按失败任务退避重试，保留原始消息，不把推理、半段 JSON 或空正文写入记忆；状态无变化必须由模型明确返回 `{}`。

## 可见范围

| 场景 | 记忆范围 | 个人状态 |
| --- | --- | --- |
| 本机桌面 | `owner`，与绑定主人私聊共享 | `owner` |
| 主人 QQ 私聊 | `owner`，可衔接桌面近期对话 | `owner` |
| 其他 QQ 私聊 | 机器人账号 + 用户 QQ | 同私聊范围 |
| 群聊 | 机器人账号 + 群号，所有本群公开发言 | 群范围 + 用户 QQ |

主人由 `BINES_OWNER_QQ_IDS` 显式绑定；群主或管理员角色不赋予主人身份。群内主人只读取本群公开记录。同一个用户在不同群、私聊的状态分别保存。远程记忆调用由网关从事件重新计算权限，模型传入的 scope 无效。

撤回消息会替换原始正文并同步到记忆服务，同时失效其派生摘要和事实。未 @ 的群消息保留为公开背景。断线期间不能保证完整回放，会话下一次回复时补取历史。

完整模式可按需启动 RAG Web 管理记忆。离线迁移、校验或重建前停止 Memory Server、网关及其客户端；旧文件迁移使用 `python -m chatBot.migrate_memory`，保留原文件。当前没有自动删除保留策略。

实现入口：`chatBot/memory_hub.py`、`chatBot/store.py`、`thingking/qq_thinking.py`、`thingking/src/memory/`。
