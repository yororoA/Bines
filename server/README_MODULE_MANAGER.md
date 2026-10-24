# 模块管理器

统一入口为根目录 `python bines.py`。Windows 可双击 `start_web_manager.bat`，macOS 可双击 `start_bines.command`。Web 模式会自动启动服务并打开 `http://127.0.0.1:5000`。

CLI 和 Web 使用同一 `ServiceSupervisor`，依赖由 `process_registry.py` 定义。启动单个服务自动补启依赖，失败后阻塞下游并保留原因，停止时先停止下游。RAG Web 按需启动，RAG 检索无需独立服务器，唱歌流水线按请求运行。

`--profile chatbot` 仅启动 QQ Python 链路，`--external-napcat` 连接现有 NapCat，`--manager-only` 只开管理页不自动启动。Ctrl+C 清理本次管理的进程和新启动的容器，保留所有数据。直接运行旧模块脚本仍保留兼容握手，但日常请使用统一入口。

完整服务清单、环境变量和故障排查说明见 [`START_HERE.md`](../START_HERE.md)。
