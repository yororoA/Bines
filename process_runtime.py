import os
import sys
from typing import Dict, List, Union


def resolve_process_command(config: Dict[str, str]) -> List[str]:
    """从进程配置解析可执行命令。"""
    if "command" in config and config["command"]:
        cmd = config["command"]
        return cmd if isinstance(cmd, list) else [str(cmd)]
    return [config["interpreter"], config["script"]]


def build_python_runtime_env(base_env: Dict[str, str] = None) -> Dict[str, str]:
    """构造统一的 Python 子进程环境（UTF-8 输出）。"""
    env = dict(base_env or os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if sys.platform == "win32":
        env["PYTHONUTF8"] = "1"
    return env
