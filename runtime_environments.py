"""Environment paths shared by the installer and application entry points."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
ENVIRONMENTS = {
    "launcher": ".venv",
    "server": "server/server_venv",
    "thinking": "thingking/thinking_venv",
    "visual": "visual/envs/runtime_venv",
    "hearing": "hearing/envs/runtime_venv",
    "singing": "singing/singing_venv",
}
SERVICE_ENVIRONMENTS = {
    "ChatBot": "launcher", "QQ Thinking": "launcher",
    "ZMQ Bus": "server", "Classification": "server", "Display": "server", "Speaking": "server",
    "Thinking": "thinking", "Bored Detector": "thinking", "RAG Server": "thinking",
    "RAG Web": "thinking", "Realtime Screen": "thinking",
    "Visual": "visual", "Hearing": "hearing",
}


def environment_python(name, root=ROOT):
    directory = Path(root) / ENVIRONMENTS[name]
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def service_python(name, root=ROOT):
    target = environment_python(SERVICE_ENVIRONMENTS[name], root)
    if target.is_file():
        return str(target)
    # Preserve existing installations that have not used the new installer.
    if name == "QQ Thinking":
        legacy = environment_python("thinking", root)
        if legacy.is_file():
            return str(legacy)
    return str(environment_python("launcher", root)) if environment_python("launcher", root).is_file() else sys.executable


def enter_launcher():
    """Use the installed launcher environment without requiring shell activation."""
    target = environment_python("launcher")
    if target.is_file() and Path(sys.prefix).absolute() != target.parent.parent.absolute():
        os.execv(str(target), [str(target), str(ROOT / "bines.py"), *sys.argv[1:]])
