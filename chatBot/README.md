# QQ ChatBot / NapCat

启动、Docker 部署、身份绑定、记忆迁移和 MCP 配置见 [START_HERE](../START_HERE.md#qq--纯-chatbot)。

- `main.py`：单连接 NapCat 网关，将原事件持久化到 SQLite。
- `messages.py`：OneBot 段与 CQ 解析，账号 / 群 / 用户身份。
- `store.py`：共享记忆、个人状态、去重、队列租约、回复轮次与发送回执。
- `capabilities.py`：统一权限与动态 schema；普通用户当前会话，主人私聊和本机完整能力。
- `catalog.py` / `api/`：随项目附带全部官方 OpenAPI，缺少 WebUI token 时仍可发现参数；运行时目录合并新增动作，明确未确认/缺失状态。
- `update_catalog.py`：`python -m chatBot.update_catalog 版本号` 更新官方目录及来源/内容哈希。
- `gateway.py` / `gateway_client.py`：令牌鉴权的本机 HTTP API，Skills 不再创建第二条 WebSocket。
- `mcp_server.py`：官方 MCP SDK stdio 服务，列出全部独立 API 工具，复用同一能力入口。
- `../thingking/qq_thinking.py`：独立 QQ Thinking，按发送者合并、并行处理，确认发送后记忆。

旧桌面 QQ 分支不再接收输入。纯 ChatBot 只启动网关与 QQ Thinking，不需要桌面依赖或模型下载。无法确认是否送达的写操作保留回执，不自动重放。
