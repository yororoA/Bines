# QQ ChatBot / NapCat

启动、Docker 部署、身份绑定、记忆迁移和 MCP 配置见 [START_HERE](../START_HERE.md#qq--纯-chatbot)。

- `main.py`：单连接 NapCat 网关，将原事件持久化到 SQLite。
- `messages.py`：OneBot 段与 CQ 解析，账号 / 群 / 用户身份。
- `store.py`：共享记忆、个人状态、去重、队列租约、回复轮次与发送回执。
- `capabilities.py`：NapCat 权限与动态 schema；普通用户限当前会话，本机和显式绑定主人（含群聊）有完整 QQ 账号操作能力。主人群聊仍不能读取主人私聊记忆或调用网站、外部 MCP。
- `catalog.py` / `api/`：随项目附带全部官方 OpenAPI，缺少 WebUI token 时仍可发现参数；运行时目录合并新增动作，明确未确认/缺失状态。
- `update_catalog.py`：`python -m chatBot.update_catalog 版本号` 更新官方目录及来源/内容哈希。
- `gateway.py` / `gateway_client.py`：令牌鉴权的本机 HTTP API，Skills 不再创建第二条 WebSocket。
- `mcp_server.py`：官方 MCP SDK stdio 服务，列出全部独立 API 工具，复用同一能力入口。
- `video.py` / `video_decode.py` 对本轮收到或实际引用的 QQ 视频用 PyAV 解码，按目标 4 fps 抽样，再由 See Now 筛选并依次填入 3×3 九宫格。每轮最多两段，每段不超过 64 MiB 或 1 小时，解码超时 45 秒。每段最多采 240 个候选、分析 8 张拼图。结果附采样范围、时间及截断说明，声音不在观察范围。
- `../thingking/qq_thinking.py`：独立 QQ Thinking，按发送者合并、并行处理，确认发送后记忆。

旧桌面 QQ 分支不再接收输入。默认 Docker ChatBot 模式同时启动独立 Memory Server 与 Memory Worker，不需要桌面音视频依赖。无法确认是否送达的写操作保留回执，不自动重放。

闲聊的停顿和标点由碧波按语境选择。发送网关保留正文里的句号、逗号、连续问叹号和非空行之间的换行，清理空白行和误抄的历史消息前缀。一段最终回复发成一个 QQ 气泡。模型要连发几条时，可以分别调用发送接口。

未产生外部操作的失败轮次按有界指数退避重试；已有操作回执或耗尽重试的轮次进入失败队列。`/health` 对本机鉴权调用返回队列、死信、QQ worker 心跳和入站落库状态；普通会话只获得连接状态。历史经历采用分批游标回填，迟到操作回执继续增量同步。

宿主机启动器持续监督网站浏览器桥。首次启动失败或桥进程退出后，以 2–60 秒退避重试；QQ 核心链路可以继续启动。桥重连复用已存在的 Chrome 会话并更新共享队列凭证，容器无需重启。若现有浏览器自身失去响应，仍暂停该会话操作，避免自动打开重复窗口。
