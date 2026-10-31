# ChatBot 记忆模式

所有渠道使用 MemoryService。默认 Docker 模式下，完整模式与纯 QQ 模式均由独立的 Memory Server 持有 `memory.db` 和 Chroma 向量索引，网关通过远端服务读写记忆。宿主机纯 QQ 模式在没有 Memory Server 时才退回网关内置的词面检索。数据目录由 `.env` 中的 `BINES_MEMORY_DIR` 指定，默认 `thingking/memory_data`。

QQ 原始消息、事件队列、回复轮次和工具发送回执放在独立的 `gateway.db`。网关确认记忆服务提交后才标记同步成功，重启时继续同步，通过来源标识去重。启动时会识别旧 NapCat `memory.db` 的表结构，先迁移为 `gateway.db`，再创建新版记忆库。

## 每轮回复

1. 从真实 OneBot 事件计算机器人账号、用户和群号；正文中的身份声明不改变权限。
2. 按当前会话补取最多 30 条 NapCat 历史，去重并同步到 MemoryService。
3. 读取最近 30 条会话记录、较早情节摘要、相关长期事实和记忆，以及当前个人状态。引用 ID 从原始消息附带。
4. 人格和场景提示词统一由 `persona.yaml` 与 `prompt_builder` 生成。QQ 输入、引用、图片观察和工具结果作为数据处理。
5. 回复确认发送成功后才记录 assistant；结果不确定时保留回执并暂停，禁止自动重复发送。

独立的 Memory Worker 对所有渠道进行状态更新、情节总结、稳定事实提取和主人日记整理。默认每积累 40 条待整合消息，总结较早的 30 条；短会话也可在空闲后触发摘要。任务和重试保存在 SQLite 中，后台模型请求不占用对话进程。

摘要、日记和状态任务沿用 `DEEPSEEK_THINKING` 的思考模式，推理与正文共享输出额度。摘要和日记默认给 8192 个 token，状态更新给 4096 个 token；摘要与状态更新使用 JSON 输出格式。截断后仅重试当前请求，重试上限由 `BINES_MODEL_RETRY_MAX_TOKENS` 控制，默认 16384；状态任务自己的重试预算仍为 8192，并受全局上限约束。仍被截断或正文为空时按失败任务退避重试，保留原始消息，不把推理、半段 JSON 或空正文写入记忆；状态无变化必须由模型明确返回 `{}`。

检索键按源数据变化增量更新，状态、任务和用量写入不会触发整库重建；源消息变化会传播到依赖的检索键。后台向量维护采用有限批次，显式重建则完整处理。旧库首次初始化会扫描一次以建立变更跟踪。

## 自动维护

- 真实完成的主人本机或私聊回合可触发自动纠错审查，范围限当前用户原话和本轮实际呈现的记忆。服务端核对身份、来源、版本和租约；第三方陈述、假设、时间变化或歧义不直接覆盖事实。
- 原始消息保留，纠错写入版本账本；依赖旧事实的派生记忆暂停召回，按依赖重建后重新建联。
- 建联使用紧凑证据编号和版本缓存；正、负判断均可缓存，缺失判断不当作负例。证据不足时自动扩大一次预算，仍不确定则保留未决记录，原文继续可检索。
- 失败任务按有限次数退避和恢复，迟到结果不能覆盖新租约。管理页显示自动纠错任务及未决关联。
- 后台记忆任务按模型请求记录 Token 用量，包括截断、重试与失败；未知用量明确标记。该账本不覆盖全部前台聊天、视觉或搜索费用。

可在 `.env` 调整以下参数，修改后重启相关服务：

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `BINES_MEMORY_ASSOCIATION_MODEL_CANDIDATES` | `6` | 单次建联送入模型的候选上限 |
| `BINES_MEMORY_RECOVERY_CYCLES` | `2` | 失败任务最多自动恢复轮数，设为 0 可关闭 |
| `BINES_MEMORY_RECOVERY_SECONDS` | `3600` | 自动恢复探测的间隔秒数 |
| `BINES_MEMORY_INDEX_BATCH_SIZE` | `64` | 后台向量维护每类操作的批次上限 |
| `BINES_MEMORY_GRAPH_EXPANSION` | `true` | 仅控制图扩展，保留同一直接检索与来源去重路径 |

已有 Memory Server 时，可独立运行整理进程：

```bash
python thingking/consolidation_worker.py --direct-memory-server
```

离线回归使用 Thinking 环境：

```bash
BINES_ENV_FILE=/dev/null thingking/thinking_venv/bin/python -m unittest discover -s thingking/tests
```

真实模型验收会产生 API 请求和费用，默认写入隔离的系统临时目录，不连接 QQ：

```bash
PYTHONPATH=.:thingking/src thingking/thinking_venv/bin/python tools/validate_memory_automation.py --trials 3
```

## 可见范围

| 场景 | 记忆范围 | 个人状态 |
| --- | --- | --- |
| 本机桌面 | `owner`，与绑定主人私聊共享 | `owner` |
| 主人 QQ 私聊 | `owner`，可衔接桌面近期对话 | `owner` |
| 其他 QQ 私聊 | 机器人账号 + 用户 QQ | 同私聊范围 |
| 群聊 | 机器人账号 + 群号，所有本群公开发言 | 群范围 + 用户 QQ |

主人由 `BINES_OWNER_QQ_IDS` 显式绑定；群主或管理员角色不赋予主人身份。群内主人只读取本群公开记录。同一个用户在不同群、私聊的状态分别保存。远程记忆调用由网关从事件重新计算权限，模型传入的 scope 无效。

新增个人事实的来源消息必须属于事实主体，模型把群成员 A 的原话归给 B 时不会写入 B 的画像。旧式无来源调用仅在单一真实用户且主体唯一匹配时兼容。

Memory RPC 默认监听 `127.0.0.1`，所有网络请求校验 `BINES_MEMORY_RPC_TOKEN`，未配置时在数据目录创建私有凭据文件。Docker 服务共享该目录；其他机器的客户端需要显式配置相同 token。管理页采用独立凭据、本机短期会话和 Host/Origin 校验，跨站页面不能调用管理 API；远程管理需配置允许的 Host 并携带 Bearer token。

撤回消息会替换原始正文并同步到记忆服务，同时失效其派生摘要和事实。未 @ 的群消息保留为公开背景。断线期间不能保证完整回放，会话下一次回复时补取历史。

完整模式可按需启动 RAG Web 管理记忆。离线迁移、校验或重建前停止 Memory Server、网关及其客户端；旧文件迁移使用 `python -m chatBot.migrate_memory`，保留原文件。当前没有自动删除保留策略。

实现入口：`chatBot/memory_hub.py`、`chatBot/store.py`、`thingking/qq_thinking.py`、`thingking/src/memory/`。
