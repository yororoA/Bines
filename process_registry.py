import os
from typing import Dict, List

from config import ROOT_DIR


def get_processes(display_script: str = "gui_display.py") -> List[Dict[str, str]]:
    """返回模块进程配置列表。通过 display_script 区分 Web/控制台显示模块入口。"""
    root = str(ROOT_DIR)
    return [
        {
            "name": "Classification",
            "script": os.path.join(root, "server", "classification_server.py"),
            "interpreter": os.path.join(root, "server", "server_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "server"),
        },
        {
            "name": "Display",
            "script": os.path.join(root, "server", display_script),
            "interpreter": os.path.join(root, "server", "server_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "server"),
        },
        {
            "name": "Speaking",
            "script": os.path.join(root, "speaking", "main.py"),
            "interpreter": os.path.join(root, "server", "server_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "speaking"),
        },
        {
            "name": "Visual",
            "script": os.path.join(root, "visual", "0.py"),
            "interpreter": os.path.join(root, "visual", "envs", "runtime_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "visual"),
        },
        {
            "name": "RAG Server",
            "script": os.path.join(root, "thingking", "rag_server.py"),
            "interpreter": os.path.join(root, "thingking", "thinking_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "thingking"),
        },
        {
            "name": "Hearing",
            "script": os.path.join(root, "hearing", "1.py"),
            "interpreter": os.path.join(root, "hearing", "envs", "runtime_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "hearing"),
        },
        {
            "name": "Bored Detector",
            "script": os.path.join(root, "thingking", "src", "bored_detector.py"),
            "interpreter": os.path.join(root, "thingking", "thinking_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "thingking", "src"),
        },
        {
            "name": "Thinking",
            "script": os.path.join(root, "thingking", "src", "handle_zmq.py"),
            "interpreter": os.path.join(root, "thingking", "thinking_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "thingking", "src"),
        },
        {
            "name": "ChatBot",
            "script": os.path.join(root, "chatBot", "main.py"),
            "interpreter": os.path.join(root, "server", "server_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "chatBot"),
        },
    ]
