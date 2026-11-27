"""Load the project .env once, independently of the working directory.

File values override inherited shell values. Missing keys still accept injected
environment values (CI/MCP); explicit blanks in the file remain blank.
"""
import os
from pathlib import Path
import re
import threading

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_lock = threading.Lock()
_loaded = False
_variable = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def env_path():
    selected = Path(os.environ.get("BINES_ENV_FILE") or PROJECT_ROOT / ".env").expanduser()
    return selected if selected.is_absolute() else PROJECT_ROOT / selected


def read_env(path):
    """Read single-line dotenv assignments without executing shell expressions."""
    path = Path(path)
    if not path.exists():
        return {}
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise ValueError(f"{path.name}:{number} 应为 KEY=value")
        key, value = match.groups()
        quoted = value[:1] in {"'", '"'}
        quote = value[:1] if quoted else ""
        if quoted:
            pattern = r"""^(['"])((?:\\.|(?!\1).)*)\1\s*(?:#.*)?$"""
            parsed = re.fullmatch(pattern, value)
            if not parsed:
                raise ValueError(f"{path.name}:{number} 引号未闭合；请使用单行值")
            value = parsed[2]
            if quote == '"':
                escapes = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
                value = re.sub(r'\\(["\\nrt])', lambda m: escapes[m[1]], value)
        else:
            value = "" if value.startswith("#") else re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if quote != "'":
            value = _variable.sub(
                lambda m: values.get(m[1], os.environ.get(m[1], m[2] or "")), value,
            )
        values[key] = value
    return values


def load_env():
    global _loaded
    with _lock:
        if not _loaded:
            os.environ.update(read_env(env_path()))
            # Explicit launcher --profile wins over the file in child processes.
            runtime_profile = os.environ.get("_BINES_LAUNCH_PROFILE")
            if runtime_profile in {"full", "chatbot"}:
                os.environ["BINES_PROFILE"] = runtime_profile
            _loaded = True


def get_env(name, default=None):
    load_env()
    return os.environ.get(name, default)
