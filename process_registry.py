import os
import sys
from typing import Dict, List, Optional
from urllib.parse import urlparse

from config import GPT_SOVITS_DIR, HTTP_PORTS, ROOT_DIR, TTS_HTTP_BASE_URL, ZMQ_PORTS

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
        "ports": [url.port or 9880],
        "command": [
            interpreter, script,
            "-a", url.hostname or "127.0.0.1",
            "-p", str(url.port or 9880),
            "-c", "GPT_SoVITS/configs/tts_infer.yaml",
        ],
    }


def get_processes(display_script: str = "gui_display.py", profile: str | None = None) -> List[Dict[str, object]]:
    """返回模块进程配置列表。通过 display_script 区分 Web/控制台显示模块入口。"""
    root = str(ROOT_DIR)
    profile = profile or os.environ.get("BINES_PROFILE", "full")
    if profile not in {"full", "chatbot"}:
        raise ValueError("BINES_PROFILE 必须为 full 或 chatbot")
    tts_service = _gpt_sovits_process()
    rows = [*([tts_service] if tts_service else []),
        {
            "name": "ZMQ Bus",
            "script": os.path.join(root, "common", "zmq_bus_server.py"),
            "interpreter": os.path.join(root, "server", "server_venv", "Scripts", "python.exe"),
            "cwd": root,
            "ports": [ZMQ_PORTS["BUS_PUBLISH"], ZMQ_PORTS["BUS_SUBSCRIBE"]],
        },
        {
            "name": "Classification",
            "script": os.path.join(root, "server", "classification_server.py"),
            "interpreter": os.path.join(root, "server", "server_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "server"),
            "ports": [ZMQ_PORTS["MODULE_READY_REP"]],
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
            "ports": [ZMQ_PORTS["VISUAL_REQREP"]],
        },
        {
            "name": "RAG Server",
            "script": os.path.join(root, "thingking", "rag_server.py"),
            "interpreter": os.path.join(root, "thingking", "thinking_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "thingking"),
            "ports": [ZMQ_PORTS["RAG_SERVER_REQREP"]],
        },
        {
            "name": "RAG Web",
            "script": os.path.join(root, "thingking", "rag_web_server.py"),
            "interpreter": os.path.join(root, "thingking", "thinking_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "thingking"),
            "ports": [HTTP_PORTS["RAG_MEMORY"]],
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
            "ports": [ZMQ_PORTS["START_THINKING_REP"]],
        },
        {
            "name": "ChatBot",
            "script": os.path.join(root, "chatBot", "main.py"),
            "interpreter": os.path.join(root, "server", "server_venv", "Scripts", "python.exe"),
            "cwd": os.path.join(root, "chatBot"),
            "ports": [int(os.environ.get("BINES_QQ_GATEWAY_PORT", "6102"))],
        },
        {
            "name": "QQ Thinking",
            "script": os.path.join(root, "thingking", "qq_thinking.py"),
            "interpreter": sys.executable,
            "cwd": root,
        },
    ]
    if profile == "chatbot":
        rows = [row for row in rows if row["name"] in {"ChatBot", "QQ Thinking"}]
    for row in rows:
        interpreter = row["interpreter"]
        if not os.path.isfile(interpreter):
            unix = str(interpreter).replace(os.path.join("Scripts", "python.exe"), os.path.join("bin", "python"))
            shared = os.path.join(root, ".venv", "Scripts" if os.name == "nt" else "bin",
                                  "python.exe" if os.name == "nt" else "python")
            row["interpreter"] = unix if os.path.isfile(unix) else shared if os.path.isfile(shared) else sys.executable
    return rows
