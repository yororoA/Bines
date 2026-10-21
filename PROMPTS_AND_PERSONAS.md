# Prompt 与人格

- 人格：唯一来源是 [`thingking/persona/persona.yaml`](thingking/persona/persona.yaml)，生成的可读版本是 [`thingking/persona/PERSONA.md`](thingking/persona/PERSONA.md)。
- 对话提示词：[`thingking/src/prompt_builder.py`](thingking/src/prompt_builder.py) 按「人格 + 场景约束（本机 / 管理员 QQ 私聊 / 其他私聊 / 群聊）+ 作用域内记忆」组装。
- 记忆整合提示词（情节摘要、日记）：[`thingking/src/memory/worker.py`](thingking/src/memory/worker.py)。
- 主动发言判断：[`thingking/src/bored_detector.py`](thingking/src/bored_detector.py)，人格与心情描述同样来自 `persona.yaml`。
