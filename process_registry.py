import os
from typing import Dict, List, Optional
from urllib.parse import urlparse

from config import GPT_SOVITS_DIR, ROOT_DIR, TTS_HTTP_BASE_URL

THINKING_STARTUP_DEPENDENCIES = frozenset({
    "Display",
    "Speaking",
    "Visual",
    "RAG Server",
    "Hearing",
})


def _gpt_sovits_process() -> Optional[Dict[str, object]]:
    """GPT-SoVITS 整合包（自带 runtime）存在时，作为可选 TTS 服务交给管理器启动。"""
    if not GPT_SOVITS_DIR:
        return None
    install_dir = os.path.abspath(GPT_SOVITS_DIR)
    interpreter = os.path.join(install_dir, "runtime", "python.exe")
    script = os.path.join(install_dir, "api_v2.py")
    if not (os.path.isfile(interpreter) and os.path.isfile(script)):
        return None
    url = urlparse(TTS_HTTP_BASE_URL)
    return {
        "name": "GPT-SoVITS",
        "script": script,
        "interpreter": interpreter,
        "cwd": install_dir,
        "command": [
            interpreter, script,
            "-a", url.hostname or "127.0.0.1",
            "-p", str(url.port or 9880),
            "-c", "GPT_SoVITS/configs/tts_infer.yaml",
        ],
    }


def get_processes(display_script: str = "gui_display.py") -> List[Dict[str, str]]:
    """返回模块进程配置列表。通过 display_script 区分 Web/控制台显示模块入口。"""
    root = str(ROOT_DIR)
    tts_service = _gpt_sovits_process()
    return [*([tts_service] if tts_service else []),
        {
            "name": "ZMQ Bus",
            "script": os.path.join(root, "common", "zmq_bus_server.py"),
            "interpreter": os.path.join(root, "server", "server_venv", "Scripts", "python.exe"),
            "cwd": root,
        },
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
            "name": "RAG Web",
            "script": os.path.join(root, "thingking", "rag_web_server.py"),
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
