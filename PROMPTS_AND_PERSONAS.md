# Prompt 与人格

- 人格：唯一来源是 [`thingking/persona/persona.yaml`](thingking/persona/persona.yaml)，生成的可读版本是 [`thingking/persona/PERSONA.md`](thingking/persona/PERSONA.md)。
- 对话提示词：[`thingking/src/prompt_builder.py`](thingking/src/prompt_builder.py) 按「人格 + 场景约束（本机 / 管理员 QQ 私聊 / 其他私聊 / 群聊）+ 作用域内记忆」组装。
- 记忆整合提示词（情节摘要、日记）：[`thingking/src/memory/worker.py`](thingking/src/memory/worker.py)。
- 自主意愿：[`common/proactive.py`](common/proactive.py) 维护交流、好奇和行动倾向；[`thingking/src/bored_detector.py`](thingking/src/bored_detector.py) 接入桌面，[`chatBot/proactive.py`](chatBot/proactive.py) 接入 QQ；执行复用现有技能。

意愿根据时间、角色情绪和有日期的交流历史缓慢变化，不再使用“无聊值攒满就开口”。近期交流产生满足感，情绪评估影响各类倾向，过去行动和回复延迟影响后续选择；旧情绪逐渐回归中性。模型依据近期对话、召回摘要、当前状态与行动结果决定具体目标，也可以选择不行动。情绪是模型依据交流推断出的角色倾向，不是用户情绪诊断。

QQ 模式以已绑定主人真实私聊作为自主身份来源，可主动选择 NapCat 接口、目标、图文组合和分条；完整模式另外可调用桌面技能。安静时段、告别或无人回复后禁止继续主动联系，独立查询和探索仍可进行。键鼠操作前检查系统输入是否空闲，每个技能执行前检查是否有新的用户活动。自主轮次和实际回执持久化，系统机会不记成用户发言，写操作回执不确定时不自动重新生成并重做。

自主意图以 `system` 消息提供，并追加明确标注为系统调度的内部 `user` 协议边界，使 DeepSeek 思考模式开始新轮次。它不代表真实用户发言，不进入用户记忆或活动记录。同轮工具调用返回的 `reasoning_content` 与 `tool_calls` 完整回传；不能仅追加 `system` 消息，否则旧助手正文会被当成当前思考轮次并因缺少推理内容返回 400。

`BINES_PROACTIVE_ENABLED` 控制开关，默认 `auto`；`BINES_PROACTIVE_INTERVAL` 默认 1200 秒，是意愿变化速度；`BINES_PROACTIVE_MIN_IDLE` 默认 600 秒。默认 01:00–08:00 不主动联系，每天最多 4 次主动联系、8 次独立行动。参数说明见 [`.env.example`](.env.example)。状态存放在 QQ 网关 `proactive_sessions/proactive_runs` 与桌面 `proactive_desktop.json`，包括情绪、兴趣、意愿强度、已执行主题与反馈。
