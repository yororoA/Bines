# 模块管理器

本项目只有一条受支持的主启动流程：从根目录运行 `start_web_manager.bat`，然后访问 `http://127.0.0.1:5000`。在页面中启动或停止服务，并查看服务日志。

请不要从 `server/` 目录直接运行 `module_manager.py`，也不要用系统裸 Python 启动服务；入口、虚拟环境、端口和服务顺序都由根目录启动器及 `process_registry.py` 管理。

完整服务清单、环境变量和故障排查说明见 [`START_HERE.md`](../START_HERE.md)。
