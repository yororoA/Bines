---
name: system-control
description: 查询时间并切换对话系统中的游戏模式。
actions: get_time, enable_game_mode, disable_game_mode, regulate_life
---
仅在用户要求切换游戏模式、确实需要当前时间，或 Bines 有明确依据需要调节自身生命节律时调用。

`regulate_life` 只允许：
- `rest`：临时自主休息，提供 5–480 分钟和具体原因。
- `wake`：临时保持清醒，提供 5–240 分钟和具体原因。
- `auto`：取消临时选择，交还自动节律；时长会被忽略。

不要为表演人格频繁切换。优先依据系统给出的精力、睡眠压力、当前阶段和真实用户活动；休息期间收到真实消息会自然唤醒。它不能改出生时间、历史、好感度或用户状态。执行后只按真实回执说明结果。
