# Prompt 与人格

- 人格：唯一来源是 [`thingking/persona/persona.yaml`](thingking/persona/persona.yaml)，生成的可读版本是 [`thingking/persona/PERSONA.md`](thingking/persona/PERSONA.md)。
- 对话提示词：[`thingking/src/prompt_builder.py`](thingking/src/prompt_builder.py) 按「人格 + 场景约束（本机 / 管理员 QQ 私聊 / 其他私聊 / 群聊）+ 作用域内记忆」组装。
- 真实时间：[`common/clock.py`](common/clock.py) 统一时间工具、对话时间戳与自主评估；`BINES_MEMORY_TIMEZONE` 默认 `Asia/Shanghai`，启动器同步配置容器 `TZ`。每次对话或工具续轮请求刷新带星期、时区和 UTC 偏移的当前时间；历史消息、日记、网页日期和旧 `current_time` 不作为现在。记忆写入仍使用原始 Unix 时间戳，不改动历史记录。
- 记忆整合提示词（情节摘要、日记）：[`thingking/src/memory/worker.py`](thingking/src/memory/worker.py)。
- 自主意愿：[`common/proactive.py`](common/proactive.py) 维护交流、好奇和行动倾向；[`thingking/src/bored_detector.py`](thingking/src/bored_detector.py) 接入桌面，[`chatBot/proactive.py`](chatBot/proactive.py) 接入 QQ；执行复用现有技能。

意愿根据时间、角色情绪和有日期的交流历史缓慢变化，不再使用“无聊值攒满就开口”。近期交流产生满足感，情绪评估影响各类倾向，过去行动和回复延迟影响后续选择；旧情绪逐渐回归中性。模型依据近期对话、召回摘要、当前状态与行动结果决定具体目标，也可以选择不行动。情绪是模型依据交流推断出的角色倾向，不是用户情绪诊断。

QQ 模式以已绑定主人真实私聊作为自主身份来源，可主动选择 NapCat 接口、目标、图文组合和分条；完整模式另外可调用桌面技能。安静时段、告别或无人回复后禁止继续主动联系，独立查询和探索仍可进行。键鼠操作前检查系统输入是否空闲，每个技能执行前检查是否有新的用户活动。自主轮次和实际回执持久化，系统机会不记成用户发言，写操作回执不确定时不自动重新生成并重做。

自主意图以 `system` 消息提供，并追加明确标注为系统调度的内部 `user` 协议边界，使 DeepSeek 思考模式开始新轮次。它不代表真实用户发言，不进入用户记忆或活动记录。同轮工具调用返回的 `reasoning_content` 与 `tool_calls` 完整回传；不能仅追加 `system` 消息，否则旧助手正文会被当成当前思考轮次并因缺少推理内容返回 400。

`BINES_PROACTIVE_ENABLED` 控制开关，默认 `auto`；`BINES_PROACTIVE_INTERVAL` 默认 1200 秒，是意愿变化速度；`BINES_PROACTIVE_MIN_IDLE` 默认 600 秒。默认 01:00–08:00 不主动联系，每天最多 4 次主动联系、8 次独立行动。参数说明见 [`.env.example`](.env.example)。状态存放在 QQ 网关 `proactive_sessions/proactive_runs` 与桌面 `proactive_desktop.json`，包括情绪、兴趣、意愿强度、已执行主题与反馈。

生命连续性由 [`thingking/src/memory/life.py`](thingking/src/memory/life.py) 的中央账本维护。它以最早可验证记录作为生命起点，按真实时区持续推进睡眠、夜间清醒、苏醒、活动和准备休息阶段，并维护运行精力、睡眠压力、清醒周期、每日真实交流/行动计数、作用域隔离的自主目标和可验证里程碑。消息、撤回、工具经历和自主结果按稳定来源幂等写入；重启不会重新出生或重复计数，已有历史会在首次启用时补录。模型只读取这个确定性状态，不能凭空编造童年、身体疾病或未发生的经历。`BINES_LIFE_*` 可配置作息，睡眠阶段不会触发自主行动。

工具记忆使用 [`common/memory_journal.py`](common/memory_journal.py) 的 SQLite 持久队列。QQ 和本机的搜索、网页读取、Skill/MCP、桌面操作、图片/引用观察、自主反思和结果反馈都会保存时间、参数、结果、来源链接及真实状态；每次操作开始前先落盘，完成后更新。失败或进程中断不会丢掉已经取得的结果，网络恢复或服务重启后补同步，来源编号与版本防止重复和旧结果覆盖。

中央 `experiences` 保存完整文本证据，`memories` 保存可检索的经历摘要；完整结果分段参与全文/语义检索，后台摘要失败时仍可检索。检索自身、时间和目录查询留审计，避免复制旧记忆。凭据字段与内联媒体数据省略。工具观察和未发送的反思不冒充用户发言，不更新用户活动、用户画像或好感度；QQ 发送由网关按真实回执写入目标会话。群、机器人账号和陌生人私聊分别隔离，绑定主人私聊与本机共享；图片和引用关联源消息，撤回后派生记忆不可重放。日记可以追溯工具证据，导出包含完整经历。启动时补录旧轮次中仍保存的自主结果和操作回执，旧版本已截断或未保存的内容无法还原。
