# ChatBot 服务

ChatBot 是纯 Python 主运行链路中的 NapCat WebSocket 客户端，由根目录服务管理器启动。完整启动、停止和故障排查步骤见 [`START_HERE.md`](../START_HERE.md)。

默认连接地址为 `ws://127.0.0.1:6101`，可通过 `BINES_NAPCAT_WS_URL` 覆盖。访问令牌只从 `BINES_NAPCAT_ACCESS_TOKEN` 环境变量读取；不要把真实令牌写进 Python 文件或文档。

模块管理器启动 ChatBot 时会继承启动器的环境变量。NapCat 服务本身是外部依赖，需要先在本机运行。
