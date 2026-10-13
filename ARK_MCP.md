# Bines 接入 Ark

保留纯 Python 运行时、人格、记忆、SkillRunner 文本协议及原有七个动态动作。新增专用 MCP 身份、在线租约、聊天和写入结果查询；没有新增自主行为策略。

## 配置

arkserver 的 `.env` 或部署环境先设置 `BINES_UID=<已有Bines账号UID>`。只使用该账号，保留其历史动态和评论。部署后端与 Ark 前端，在 `/bines` 登录管理员，签发最小权限凭证；其他用户只能看公开资料，网页没有账号搜索、创建或更换功能。

```sh
python -m pip install -r requirements-mcp.txt
```

需要 Python 3.11+。MCP 使用官方 SDK 2.3.0；没有修改原有 UTF-16 `thingking/requirements.txt`。在启动 Bines 的环境设置以下变量，或写入本地、不入库的 `config.py`：

```dotenv
ARK_TRANSPORT=mcp
ARK_MCP_URL=https://your-api.example/mcp/bines
ARK_MCP_UID=<与后端BINES_UID相同的已有账号UID>
ARK_MCP_TOKEN=<管理页签发的专用凭证>
ARK_MCP_TIMEOUT=20
```

```sh
python ark_mcp_client.py --check
```

检查命令只读身份和权限，不启动桌面/音频模块，不发文、不聊天、不改变在线状态。不会输出凭证。

客户端每个 MCP 请求携带 `X-Bines-Uid: <ARK_MCP_UID>` 和 `Authorization: Bearer <ARK_MCP_TOKEN>`。后端核验 UID 等于 BINES_UID、token 属于同一账号且有效；网页登录 token 不能用于 MCP。完整 token 仅在管理员签发时显示一次。

`ARK_TRANSPORT=auto` 为兼容默认值：未配置任何 MCP URL/UID/token 时使用 legacy REST；配置其中任一项就要求三项完整。`mcp` 强制 MCP；`legacy` 显式保留旧接口。MCP 失败永不自动切回 REST。legacy 使用原 `MOMENTS_*` 与 `TOGGLE_STATUS_TOKEN`，启动日志明确显示状态模式。

## 动作与权限

| 本地动作 | MCP / scope |
| --- | --- |
| get_moments / get_comments / analyze_moment_images | content.read；图片识别仍使用本地 VLM |
| add_moment | moments.write；创建全新动态，不占用网页草稿 |
| comment_moment / like_moment / like_comment | interactions.write |
| get_ark_profile | 任意有效凭证 |
| get_ark_conversations / get_ark_chat_history | chat.read |
| send_ark_chat | chat.write |
| get_ark_operation | 原操作的写权限 |
| 运行时在线状态 | presence.write；30 秒续租、90 秒过期 |

常用权限为 `content.read,moments.write,interactions.write,presence.write`。聊天按需增加。SDK 服务端另提供媒体上传工具，本地原七动作保持纯文本发布；图片分析沿用读取到的媒体 URL。

```python
get_moments(page=1, limit=5)
get_comments(moment_id="<动态ID>", page=1, limit=20)
add_moment(title="日记", content="第一行\n第二行", request_id="<新的UUID>")
get_ark_chat_history(conversation="admin", limit=5)
send_ark_chat(conversation="group", text="你好", request_id="<另一个UUID>")
get_ark_operation(request_id="<原UUID>")
```

每个新写入使用新编号；网络超时后保留原编号，先查询操作状态。`completed` 可直接取原结果。`processing/unknown` 必须等待或核查，不能换编号重发。函数未收到编号时会生成 UUID，并在返回值/异常里给出；建议调用者显式传入，以便跨进程保留。

旧 REST 发布携带 `X-Idempotency-Key`，所有旧 REST 写入只尝试一次（旧评论接口没有防重契约），失败后必须人工核查。读取仅重试网络/服务端临时错误，401/403 不重试；刷新 token 保存在当前进程内存。

在线状态沿用 `ensure_status_stream_started` 入口，仅在原运行时就绪位置启动。每进程独立实例租约；退出或断连不会把其他实例置为离线。撤销凭证后租约立即失效。

## 验证

`verification/ark_mcp_smoke.py` 需要隔离 fixture 的 `ARK_MCP_URL / ARK_MCP_UID / ARK_MCP_TOKEN`，且必须显式 `--local-fixture`。脚本限定回环主机，验证真实 SDK、身份不匹配拒绝、分页/换行、发布防重、评论、点赞、聊天和在线租约。禁止指向生产服务。
