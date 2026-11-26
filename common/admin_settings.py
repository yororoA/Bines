"""Authenticated control-centre configuration storage.

This module deliberately does not import application configuration or load .env:
the catalogue is discovered from source literals, and editing a host installation
from Docker must never replace the container's running environment.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from functools import lru_cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, jsonify, request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MASK = "••••••••"
MAX_BYTES = 1024 * 1024
_LOCK = threading.RLock()
_ASSIGN = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
_EXAMPLE = re.compile(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]+)\s*=\s*(.*)$")
_PREFIXES = ("BINES_", "DEEPSEEK_", "HEARING_", "SINGING_", "SOVITS_", "GPT_SOVITS_", "ARK_", "MOMENTS_", "VOCU_", "HF_", "RAG_", "VISUAL_", "NAPCAT_", "TOGGLE_STATUS_", "AGENT_BROWSER_")
_READONLY = {"BINES_CONFIG_ROOT", "BINES_ENV_FILE", "BINES_DOCKER_MODE", "BINES_ADMIN_LOCAL_PROXY", "BINES_MODE"}
_SECRET = re.compile(r"(?:API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)(?:_|$)")
_DOCS = {
    "mcp": "mcp.servers.json",
    "screen": "server/realtime_screen_config.json",
    "skills": "server/skill_actions.json",
}
_GROUPS = {
    "models": "模型与搜索", "qq": "QQ 与 NapCat", "memory": "记忆与检索",
    "autonomy": "自主行为与生命节律", "website": "网站浏览器", "integrations": "MCP、Ark 与动态",
    "media": "视觉、语音与歌唱", "runtime": "启动、网络与存储",
}
_CHOICES = {
    "DEEPSEEK_THINKING": ["disabled", "enabled"],
    "DEEPSEEK_VISION_DETAIL": ["low", "high", "original", "auto"],
    "BINES_PROFILE": ["full", "chatbot"], "BINES_NAPCAT_DEPLOY": ["auto", "external"],
    "BINES_QQ_GROUP_REPLY": ["mention", "all"], "BINES_MEMORY_BACKEND": ["auto", "remote", "embedded"],
    "BINES_AUTONOMY_BACKEND": ["redis", "sqlite"], "BINES_PROACTIVE_ENABLED": ["auto", "true", "false"],
    "ARK_TRANSPORT": ["auto", "mcp", "legacy"],
}
_RANGES = {
    "DEEPSEEK_SEARCH_COUNT": (1, 50), "BINES_MODEL_RETRY_MAX_TOKENS": (1, 262144),
    "BINES_QQ_TEMPERATURE": (0, 2), "BINES_QQ_FOLLOWUP_WAIT_SECONDS": (1, 10),
    "BINES_QQ_FOLLOWUP_WAIT_TOTAL": (0, 60), "BINES_QQ_FOLLOWUP_WAIT_REQUESTS": (0, 3),
    "BINES_MEMORY_CANDIDATES": (10, 100), "BINES_MEMORY_HISTORY_SEGMENT_EXTRA": (0, 30),
    "BINES_MEMORY_MAX_LEASE_RECOVERIES": (0, 20), "BINES_MEMORY_RECOVERY_CYCLES": (0, 4),
    "BINES_MEMORY_RECOVERY_SECONDS": (300, None), "BINES_MEMORY_INDEX_BATCH_SIZE": (1, 512),
    "BINES_MEMORY_IDLE_SUMMARY_SECONDS": (60, None), "BINES_MEMORY_IDLE_SUMMARY_MIN": (2, 40),
    "BINES_MEMORY_ASSOCIATION_CANDIDATES": (1, 40), "BINES_MEMORY_ASSOCIATION_MODEL_CANDIDATES": (1, 12),
    "BINES_MEMORY_BACKFILL_BATCH": (1, 200), "BINES_MEMORY_GRAPH_MIN_ACTIVATION": (0, None),
    "BINES_MEMORY_GRAPH_FUSION_WEIGHT": (0, 1), "BINES_MEMORY_COLD_FALLBACK_LIMIT": (0, 10),
    "BINES_MEMORY_USAGE_HALF_LIFE_DAYS": (1, None), "BINES_MEMORY_EXPLORATION_K": (0, 3),
    "BINES_MEMORY_DIARY_BATCH": (1, 128), "BINES_MEMORY_DIARY_BACKFILL_BATCH": (1, 2048),
    "BINES_LIFE_WAKE_WINDOW": (1, 6), "BINES_LIFE_WIND_DOWN": (1, 6),
    "BINES_LIFE_REST_RECOVERY_MINUTES": (15, 120),
    "BINES_LIFE_IDLE_ENERGY_PER_HOUR": (0, .1), "BINES_LIFE_WORK_ENERGY_PER_HOUR": (0, .1),
    "BINES_LIFE_AUTO_REST_ENERGY": (0, 1), "BINES_LIFE_AUTO_REST_PRESSURE": (0, 1),
}


class ConfigurationError(ValueError):
    status = 400


class RevisionConflict(ConfigurationError):
    status = 409


def _group(key):
    if key.startswith("DEEPSEEK_") or key.startswith("BINES_MODEL_"):
        return "models"
    if any(part in key for part in ("QQ", "NAPCAT", "OWNER_", "BOT_QQ")):
        return "qq"
    if any(part in key for part in ("PROACTIVE", "ACTIVITY", "LIFE_", "AUTONOMY", "REDIS_")):
        return "autonomy"
    if any(part in key for part in ("WEBSITE", "BROWSER", "CHROME", "NODE_BINARY", "NPM_")):
        return "website"
    if any(part in key for part in ("MEMORY", "RAG_", "EMBEDDING", "EXPRESSION", "QDRANT", "HF_")):
        return "memory"
    if key.startswith(("ARK_", "MOMENTS_", "TOGGLE_STATUS_")) or key == "BINES_MCP_CONFIG":
        return "integrations"
    if key.startswith(("HEARING_", "SINGING_", "SOVITS_", "GPT_SOVITS_", "VOCU_", "VISUAL_")) or key == "BINES_TTS_URL":
        return "media"
    return "runtime"


def _literal_type(value):
    if isinstance(value, bool) or str(value).lower() in {"true", "false"}:
        return "boolean"
    if value is not None and re.fullmatch(r"-?\d+", str(value)):
        return "integer"
    if value is not None and re.fullmatch(r"-?(?:\d*\.\d+|\d+\.\d*)", str(value)):
        return "number"
    return "string"


@lru_cache(maxsize=1)
def settings_schema():
    """Only source literals form the allowlist; never inspect users' .env here."""
    entries = {}
    comments = []
    example = PROJECT_ROOT / ".env.example"
    if example.exists():
        for number, line in enumerate(example.read_text(encoding="utf-8").splitlines(), 1):
            match = _EXAMPLE.match(line)
            if match:
                key, value = match.groups()
                entries[key] = {"key": key, "example": value, "description": "\n".join(comments),
                                "sources": [f".env.example:{number}"]}
                continue  # Consecutive settings share the preceding explanation.
            if not line.strip():
                comments = []
            elif line.lstrip().startswith("#"):
                comment = line.lstrip()[1:].strip()
                if comment and not set(comment) <= {"="}:
                    comments.append(comment)
    roots = [PROJECT_ROOT / name for name in ("common", "tools", "chatBot", "thingking/src", "thingking/scripts", "hearing", "visual", "speaking", "singing", "server")]
    paths = list(PROJECT_ROOT.glob("*.py")) + list((PROJECT_ROOT / "thingking").glob("*.py"))
    for root in roots:
        if not root.exists():
            continue
        for directory, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if not (name.startswith(".") or "venv" in name or name in {"node_modules", "__pycache__", "vendor", "models", "static", "work"})]
            paths.extend(Path(directory) / name for name in files if name.endswith(".py"))
    for path in sorted(set(paths)):
        if path == Path(__file__):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = ast.unparse(node.func)
            if name not in {"os.getenv", "os.environ.get", "get_env", "environ.get"}:
                continue
            if not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            key = node.args[0].value
            if not key.startswith(_PREFIXES):
                continue
            entry = entries.setdefault(key, {"key": key, "description": "", "sources": []})
            entry["sources"].append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
            if len(node.args) > 1:
                try:
                    value = ast.literal_eval(node.args[1])
                    if value is None or isinstance(value, (str, int, float, bool)):
                        entry.setdefault("default", value)
                except (ValueError, TypeError):
                    entry.setdefault("default_expression", ast.unparse(node.args[1]))
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in {"int", "float"}:
                entry["type"] = "integer" if parent.func.id == "int" else "number"
    # Compose also consumes settings that have no Python getter. Its generated
    # build hashes are launcher outputs rather than configuration inputs.
    compose = PROJECT_ROOT / "deploy/docker/compose.yaml"
    if compose.is_file():
        for number, line in enumerate(compose.read_text(encoding="utf-8").splitlines(), 1):
            for key, default in re.findall(r"\$\{([A-Z][A-Z0-9_]+):-([^}]*)}", line):
                if key.startswith(_PREFIXES) and not key.endswith("_HASH"):
                    entry = entries.setdefault(key, {"key": key, "description": "", "sources": []})
                    entry["sources"].append(f"deploy/docker/compose.yaml:{number}")
                    # A Compose override may differ from the local Python default.
                    entry.setdefault("default", default)
    # These are deployment controls, displayed for transparency but not editable.
    for key in _READONLY:
        entry = entries.setdefault(key, {"key": key, "sources": []})
        entry["description"] = {
            "BINES_CONFIG_ROOT": "管理服务启动时指定的配置目录。",
            "BINES_ENV_FILE": "启动服务前指定的环境文件，由加载器在读取文件前选择。",
            "BINES_DOCKER_MODE": "启动器在混合运行模式下自动注入。",
            "BINES_ADMIN_LOCAL_PROXY": "由容器部署注入，用于识别本地管理连接。",
            "BINES_MODE": "由容器部署注入，用于选择主实例或分身人格。",
        }[key] + " 此项在控制中心只读。"
    for key, field in entries.items():
        value = field.get("default", field.get("example"))
        field.setdefault("default", None)
        field.setdefault("type", _literal_type(value))
        if key in _CHOICES:
            field.update(type="string", choices=_CHOICES[key])
        # Identifiers, revisions and shell command arguments remain strings.
        if key.endswith("_TOKENS"):
            field["type"] = "integer"
        if key in {"BINES_EMBEDDING_OFFLINE"}:
            field["type"] = "boolean"
        if key.endswith(("_QQ_ID", "_QQ_IDS", "_REVISION", "_TOKEN", "API_KEY", "_ACCOUNT")):
            field["type"] = "string"
        if key.endswith("_PORT"):
            field.update(type="integer", minimum=1, maximum=65535)
        elif key in {"BINES_GID", "BINES_UID", "NAPCAT_GID", "NAPCAT_UID"}:
            field.update(type="integer", minimum=0, maximum=4294967294)
        elif key in _RANGES:
            lower, upper = _RANGES[key]
            field.update(minimum=lower)
            if upper is not None:
                field["maximum"] = upper
        elif key in {"BINES_LIFE_SLEEP_START", "BINES_LIFE_WAKE_HOUR", "BINES_PROACTIVE_QUIET_START", "BINES_PROACTIVE_QUIET_END"}:
            field.update(type="integer", minimum=0, maximum=23)
        elif field["type"] in {"integer", "number"}:
            field["minimum"] = 0
            if any(part in key for part in ("TIMEOUT", "WORKERS", "MAX_ATTEMPTS", "BATCH_SIZE", "BEAM_SIZE")):
                field["exclusiveMinimum"] = 0
        field.update(label=key, group=_group(key), secret=bool(_SECRET.search(key)), editable=key not in _READONLY, restart_required=True)
        if field["secret"]:
            field.update(type="secret", default=None)
            field.pop("example", None)
        if not field["description"]:
            field["description"] = "应用读取的配置项；保存后需重启使用它的服务。"
        if "default_expression" in field and field["default"] is None:
            field["description"] += "\n默认值由其他设置或运行环境计算。"
    return entries


def _contains_secret(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return bool(parsed.username or parsed.password or re.search(r"(?:token|key|secret|password|signature)=", parsed.query, re.I))
    except ValueError:
        return True


def _env_parse(raw):
    """Parse without interpolation: saved text and process environment are separate."""
    result = {}
    for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ASSIGN.fullmatch(line)
        if not match:
            raise ConfigurationError(f".env 第 {number} 行格式错误，请先修复为 KEY=value")
        value = match[4].strip()
        if value[:1] in {"'", '"'}:
            quoted = re.fullmatch(r'''^(['"])((?:\\.|(?!\1).)*)\1\s*(?:#.*)?$''', value)
            if not quoted:
                raise ConfigurationError(f".env 第 {number} 行引号未闭合")
            value = quoted[2]
            if quoted[1] == '"':
                escapes = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
                value = re.sub(r'\\(["\\nrt])', lambda m: escapes[m[1]], value)
        else:
            value = "" if value.startswith("#") else re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        result[match[2]] = value
    return result


def _coerce(field, value, validating=False):
    kind, key = field["type"], field["key"]
    try:
        if not isinstance(value, (str, int, float, bool)) or any(char in str(value) for char in "\r\n\x00"):
            raise ValueError("须为单行文本或数值")
        if len(str(value)) > 16384:
            raise ValueError("内容过长")
        if kind == "boolean":
            if str(value).lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError("须为 true 或 false")
            value = str(value).lower() in {"true", "1", "yes", "on"}
        elif kind in {"integer", "number"}:
            if isinstance(value, bool):
                raise ValueError("须为数字")
            number = float(value)
            if not math.isfinite(number) or (kind == "integer" and not number.is_integer()):
                raise ValueError("须为有限整数" if kind == "integer" else "须为有限数字")
            value = int(number) if kind == "integer" else number
            if validating:
                if "minimum" in field and value < field["minimum"]:
                    raise ValueError(f"不得小于 {field['minimum']}")
                if "exclusiveMinimum" in field and value <= field["exclusiveMinimum"]:
                    raise ValueError(f"须大于 {field['exclusiveMinimum']}")
                if "maximum" in field and value > field["maximum"]:
                    raise ValueError(f"不得大于 {field['maximum']}")
        else:
            value = str(value)
        if validating:
            if "choices" in field and value not in field["choices"]:
                raise ValueError("请选择支持的值")
            if key == "BINES_MEMORY_TIMEZONE":
                ZoneInfo(value)
            if key in {"BINES_OWNER_QQ_IDS", "BINES_BOT_QQ_ID"} and value and not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", value):
                raise ValueError("须为十进制 QQ 号（多个使用英文逗号）")
            if key.endswith("_URL") and value and "${" not in value:
                parts = urlsplit(value)
                schemes = {"redis", "rediss"} if "REDIS" in key else {"ws", "wss"} if "_WS_" in key else {"http", "https"}
                if parts.scheme not in schemes or not parts.netloc or any(c.isspace() for c in value):
                    raise ValueError("URL 的协议或主机无效")
        return value
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError) as exc:
        if not validating:
            return value
        # Never put values or parsing exceptions containing credentials in errors.
        detail = str(exc)
        if not detail.startswith(("须", "内容过长", "不得", "请选择", "URL 的")):
            detail = "格式不正确"
        raise ConfigurationError(f"{key}：{detail}") from None


def _env_string(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _quoted(value):
    # GUI values are literal except explicit dotenv references. Double quotes
    # also provide the loader's escaping for apostrophes and trailing slashes.
    if "'" in value or "${" in value or "\\" in value:
        return json.dumps(value, ensure_ascii=False)
    return "'" + value + "'"


def _rewrite_env(raw, changes, unset):
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines, seen = [], set()
    for line in text.splitlines(keepends=True):
        match = _ASSIGN.fullmatch(line.rstrip("\r\n"))
        if not match or match[2] not in changes and match[2] not in unset:
            lines.append(line)
            continue
        key = match[2]
        if key in unset:
            continue
        seen.add(key)
        # Preserve trailing comments and spacing, including exported assignments.
        original = match[4]
        if original.lstrip().startswith(("'", '"')):
            suffix = re.fullmatch(r'''\s*(['"])(?:\\.|(?!\1).)*\1(\s*(?:#.*)?)''', original)
            comment = suffix[2] if suffix else ""
        else:
            suffix = re.search(r"(\s+#.*)$", original)
            comment = suffix[1] if suffix else ""
        ending = newline if line.endswith(("\r", "\n")) else ""
        lines.append(match[1] + key + match[3] + _quoted(changes[key]) + comment + ending)
    for key, value in changes.items():
        if key not in seen:
            if lines and not lines[-1].endswith(("\r", "\n")):
                lines[-1] += newline
            lines.append(key + "=" + _quoted(value) + newline)
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    return bom + "".join(lines).encode("utf-8")


def _revision(raw, exists):
    return hashlib.sha256((b"present\0" if exists else b"absent\0") + raw).hexdigest()


def _atomic_write(path, raw, *, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if not private and path.exists():
                os.chmod(name, path.stat().st_mode & 0o777)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SettingsStore:
    def __init__(self, root=None):
        configured_root = root or os.environ.get("BINES_CONFIG_ROOT")
        self.root = Path(configured_root or PROJECT_ROOT).expanduser().resolve()
        self.docker = os.getenv("BINES_ADMIN_LOCAL_PROXY") == "docker"
        # An explicit management root describes the installation being edited
        # (including a host mount or fixture), independently of the admin process.
        self.env_file = Path(".env") if configured_root else Path(os.getenv("BINES_ENV_FILE") or ".env").expanduser()
        self.mcp_file = Path(os.getenv("BINES_MCP_CONFIG") or _DOCS["mcp"])
        if self.docker and self.mcp_file.is_absolute() and self.mcp_file.is_relative_to(PROJECT_ROOT):
            self.mcp_file = self.mcp_file.relative_to(PROJECT_ROOT)

    def _document_path(self, document_id):
        if document_id == "persona":
            from thingking.src.persona_profile import active_persona_path
            return active_persona_path().relative_to(PROJECT_ROOT)
        if document_id not in _DOCS:
            raise ConfigurationError("未知配置文档")
        if document_id == "mcp":
            if self.mcp_file.suffix.lower() != ".json":
                raise ConfigurationError("当前 BINES_MCP_CONFIG 须指向配置目录中的 MCP JSON 文件")
            return self.mcp_file
        return Path(_DOCS[document_id])

    def _path(self, relative):
        path = self.root / relative
        # Do not follow links outside the fixed administration root.
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            raise ConfigurationError("配置文件不能指向配置目录之外") from None
        return path

    def _read(self, relative):
        path = self._path(relative)
        if not path.exists():
            return b"", False
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ConfigurationError("配置文件过大或不是普通文件")
        return path.read_bytes(), True

    @contextmanager
    def _locked(self):
        with _LOCK:
            lock_path = self._path("thingking/memory_data/.admin-settings.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as stream:
                if os.name == "nt":
                    import msvcrt
                    stream.write(b"\0")
                    stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream, fcntl.LOCK_UN)

    def settings(self):
        raw, exists = self._read(self.env_file)
        saved = _env_parse(raw)
        groups = {name: {"id": name, "title": title, "fields": []} for name, title in _GROUPS.items()}
        for key, source in sorted(settings_schema().items()):
            field = copy.deepcopy(source)
            persisted, current = saved.get(key), os.environ.get(key)
            secret = field["secret"] or _contains_secret(persisted) or _contains_secret(current)
            field.update(secret=secret, configured=key in saved, source="saved" if key in saved else "default")
            field["value"] = None if secret or key not in saved else _coerce(field, persisted)
            field["default"] = None if secret or field["default"] is None else _coerce(field, field["default"])
            field["runtime"] = {"verified": False, "configured": bool(current), "value": None if secret or current is None else _coerce(field, current),
                                "note": "仅管理服务当前环境，未验证其他进程是否已加载；容器值可能与宿主配置不同。"}
            if secret:
                field.update(type="secret", default=None)
                field.pop("example", None)
            groups[field.pop("group")]["fields"].append(field)
        return {"revision": _revision(raw, exists), "groups": list(groups.values()),
                "source": {"root": str(self.root), "env_file": str(self._path(self.env_file)),
                           "mode": "docker-host" if self.docker else "host",
                           "note": "重启读取此配置的服务后生效，Docker 需重新创建对应容器。可用 ${NAME} 引用其他环境变量。动态默认值由对应程序决定。"}}

    def update_settings(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"revision", "changes", "unset"}:
            raise ConfigurationError("请求须包含 revision、changes 和可选 unset")
        changes, unset = payload.get("changes", {}), payload.get("unset", [])
        if not isinstance(changes, dict) or not isinstance(unset, list) or not all(isinstance(k, str) for k in unset):
            raise ConfigurationError("changes 须为对象，unset 须为设置名数组")
        if set(changes) & set(unset):
            raise ConfigurationError("同一项不能同时保存和恢复默认")
        schema = settings_schema()
        for key in list(changes) + unset:
            if key not in schema or not schema[key]["editable"]:
                raise ConfigurationError(f"不允许修改设置：{key}")
        normalized = {key: _env_string(_coerce(schema[key], value, True)) for key, value in changes.items()}
        with self._locked():
            raw, exists = self._read(self.env_file)
            _env_parse(raw)
            if payload.get("revision") != _revision(raw, exists):
                raise RevisionConflict("配置已被其他窗口或进程修改，请刷新后重试")
            updated = _rewrite_env(raw, normalized, set(unset))
            _env_parse(updated)
            if len(updated) > MAX_BYTES:
                raise ConfigurationError("保存后的 .env 超过 1 MiB")
            if updated != raw:
                _atomic_write(self._path(self.env_file), updated, private=True)
            result = self.settings()
            result.update(changed=sorted(set(changes) | set(unset)), restart_required=updated != raw)
            return result

    def _document(self, document_id):
        relative = self._document_path(document_id)
        raw, exists = self._read(relative)
        if exists:
            try:
                if document_id == "persona":
                    import yaml
                    content = yaml.safe_load(raw.decode("utf-8-sig"))
                else:
                    content = json.loads(raw)
            except Exception:
                raise ConfigurationError("配置文档格式无效；请先修复原文件，避免覆盖内容") from None
            if not isinstance(content, dict):
                raise ConfigurationError("配置文档根节点须为对象")
            if document_id == "mcp" and not isinstance(content.get("mcpServers"), dict):
                raise ConfigurationError("当前文件缺少 mcpServers 配置，请检查 BINES_MCP_CONFIG")
        elif document_id == "screen":
            from realtime_screen_config import DEFAULT_REALTIME_SCREEN_CONFIG
            content = dict(DEFAULT_REALTIME_SCREEN_CONFIG)
        else:
            content = {"mcpServers": {}} if document_id == "mcp" else {"actions": []} if document_id == "skills" else {}
        return content, _revision(raw, exists), exists

    def document(self, document_id):
        content, revision, exists = self._document(document_id)
        path = self._document_path(document_id)
        secret_paths = []
        if document_id == "mcp":
            content = _mask_mcp(content, secret_paths)
        notes = {
            "mcp": "凭据占位符会保留原值。修改 BINES_MCP_CONFIG 后，重启管理服务以编辑新路径。",
            "persona": "人格以 persona.yaml 为准，下次加载人格时生效。可用 render_doc.py 更新派生说明 PERSONA.md。",
            "skills": "技能开关在下次读取动作列表时生效。",
            "screen": "在宿主机的服务控制页应用保存的配置。",
        }
        if self.docker and document_id == "mcp":
            notes["mcp"] += " Docker 的 QQ Thinking 使用宿主配置文件。保存后重新运行 python bines.py，使容器挂载新版本。"
        if document_id == "persona" and path.name != "persona.yaml":
            notes["persona"] = f"分身人格以 {path.as_posix()} 为准；在工作区修改后重新启动分身。"
        elif self.docker and document_id in {"persona", "skills"}:
            notes[document_id] += " 已保存到宿主配置目录。使用镜像内配置的容器须重新构建镜像并重新创建。"
        return {"id": document_id, "path": str(path), "revision": revision, "exists": exists,
                "format": "json", "content": content, "secret_paths": secret_paths,
                "restart_required": self.docker and document_id != "screen",
                "note": notes[document_id]}

    def update_document(self, document_id, payload):
        if not isinstance(payload, dict) or set(payload) - {"revision", "content"} or not isinstance(payload.get("content"), dict):
            raise ConfigurationError("请求须包含 revision 与 content 对象")
        # Enforce finite JSON and a size limit before touching storage.
        try:
            candidate = json.dumps(payload["content"], ensure_ascii=False, allow_nan=False)
            if len(candidate.encode("utf-8")) > MAX_BYTES:
                raise ValueError()
        except (ValueError, TypeError):
            raise ConfigurationError("配置须为有效 JSON 对象，且大小不超过 1 MiB") from None
        with self._locked():
            old, revision, _ = self._document(document_id)
            if payload.get("revision") != revision:
                raise RevisionConflict("配置已被其他窗口或进程修改，请刷新后重试")
            content = copy.deepcopy(payload["content"])
            if document_id == "mcp":
                content = _restore_mcp(content, old)
                _validate_mcp(content)
            elif document_id == "screen":
                content = _validate_screen(content)
            elif document_id == "skills":
                _validate_skills(content)
            elif document_id == "persona":
                _validate_persona(content)
            if document_id == "persona":
                import yaml
                encoded = yaml.safe_dump(content, allow_unicode=True, sort_keys=False).encode("utf-8")
            else:
                encoded = (json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > MAX_BYTES:
                raise ConfigurationError("保存后的配置文档超过 1 MiB")
            _atomic_write(self._path(self._document_path(document_id)), encoded, private=document_id == "mcp")
            return self.document(document_id)


def _secret_path(path, key, value):
    # Stdio args may embed secrets in scripts, positional arguments, or flags.
    # Treat the array as one editable secret so retained values never shift to
    # a different flag when arguments are added or reordered.
    if len(path) == 2 and path[0] == "mcpServers" and key == "args" and value:
        return True
    return (len(path) >= 3 and path[-1] in {"env", "headers"}) or bool(_SECRET.search(str(key).upper())) or _contains_secret(value)


def _mask_mcp(content, paths, path=()):
    if isinstance(content, dict):
        result = {}
        for key, value in content.items():
            current = path + (key,)
            if _secret_path(path, key, value) and value is not None:
                result[key] = MASK
                paths.append("/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in current))
            else:
                result[key] = _mask_mcp(value, paths, current)
        return result
    if isinstance(content, list):
        return [_mask_mcp(value, paths, path + (str(index),)) for index, value in enumerate(content)]
    if _contains_secret(content):
        paths.append("/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path))
        return MASK
    return content


def _restore_mcp(content, old, path=()):
    if isinstance(content, dict):
        result = {}
        for key, value in content.items():
            previous = old.get(key) if isinstance(old, dict) else None
            if value == MASK:
                if previous is None or not _secret_path(path, key, previous):
                    raise ConfigurationError("新凭据请填写真实值，不能使用保留值占位符")
                result[key] = previous
            else:
                result[key] = _restore_mcp(value, previous, path + (key,))
        return result
    if isinstance(content, list):
        return [_restore_mcp(value, old[index] if isinstance(old, list) and index < len(old) else None, path + (str(index),)) for index, value in enumerate(content)]
    if content == MASK:
        if not _contains_secret(old):
            raise ConfigurationError("新凭据请填写真实值，不能使用保留值占位符")
        return old
    return content


def _validate_mcp(content):
    servers = content.get("mcpServers")
    if not isinstance(servers, dict):
        raise ConfigurationError("mcpServers 须为服务器对象")
    for name, server in servers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(server, dict):
            raise ConfigurationError("MCP 服务器名称与配置格式无效")
        if "disabled" in server and not isinstance(server["disabled"], bool):
            raise ConfigurationError("MCP disabled 须为布尔值")
        if "command" in server:
            if not isinstance(server["command"], str) or not server["command"].strip():
                raise ConfigurationError("MCP command 须为非空字符串")
            args = server.get("args", [])
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise ConfigurationError("MCP args 须为字符串数组")
        else:
            url = server.get("url")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError()
            except ValueError:
                raise ConfigurationError("MCP 须配置 command 或有效的 HTTP(S) url") from None
        for key in ("env", "headers"):
            mapping = server.get(key, {})
            if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
                raise ConfigurationError(f"MCP {key} 须为字符串键值对象")
        timeout = server.get("timeout", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigurationError("MCP timeout 须为正数")


def _validate_screen(content):
    from realtime_screen_config import DEFAULT_REALTIME_SCREEN_CONFIG, normalize_realtime_screen_config
    limits = {"interval_sec": (3, 120), "min_change_ratio": (0, 1), "capture_fps": (1, 12), "result_ttl_seconds": (15, 300)}
    if set(content) - set(DEFAULT_REALTIME_SCREEN_CONFIG):
        raise ConfigurationError("实时屏幕配置包含未知字段")
    if "enabled" in content and not isinstance(content["enabled"], bool):
        raise ConfigurationError("实时屏幕 enabled 须为布尔值")
    for key, (lower, upper) in limits.items():
        if key in content:
            value = content[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper or key in {"interval_sec", "result_ttl_seconds"} and int(value) != value:
                raise ConfigurationError(f"实时屏幕 {key} 须在 {lower}–{upper} 之间" + ("且为整数" if key in {"interval_sec", "result_ttl_seconds"} else ""))
    for key, choices in {"backend": {"see_now", "legacy"}, "quality": {"low", "standard", "high"}}.items():
        if key in content and (not isinstance(content[key], str) or content[key] not in choices):
            raise ConfigurationError(f"实时屏幕 {key} 取值无效")
    return normalize_realtime_screen_config(content)


def _validate_skills(content):
    actions = content.get("actions")
    if not isinstance(actions, list):
        raise ConfigurationError("技能配置 actions 须为数组")
    names = set()
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("name"), str) or not action["name"].strip() or not isinstance(action.get("enabled"), bool):
            raise ConfigurationError("每个技能须有非空 name 和布尔 enabled")
        if action["name"] in names:
            raise ConfigurationError("技能名称不得重复")
        names.add(action["name"])
        if "description" in action and not isinstance(action["description"], str):
            raise ConfigurationError("技能 description 须为字符串")


def _validate_persona(content):
    if not content:
        raise ConfigurationError("人格配置不可为空")
    # The model's actual prompt/document renderers are pure for supplied content.
    # This validates required structures without loading the installed persona.
    from thingking.src.persona_profile import character_prompt, output_rules, qq_output_rules, render_markdown
    identity = content.get("identity")
    if not isinstance(identity, dict) or not all(isinstance(identity.get(key), str) and identity[key].strip() for key in ("name", "model", "full_name", "species")):
        raise ConfigurationError("人格 identity 须包含非空 name、model、full_name、species")
    for key in ("aliases", "languages"):
        if not isinstance(identity.get(key), list) or not all(isinstance(v, str) for v in identity[key]):
            raise ConfigurationError(f"人格 identity.{key} 须为字符串数组")
    for key in ("speech_style", "output_format"):
        if not isinstance(content.get(key), list) or not all(isinstance(v, str) for v in content[key]):
            raise ConfigurationError(f"人格 {key} 须为字符串数组")
    try:
        character_prompt(content)
        output_rules(content)
        qq_output_rules(content)
        render_markdown(content)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ConfigurationError("人格结构不完整；请保留模型提示所需的 identity、appearance、personality、reactions、capability_boundaries、voice_guard、proactive 与 diary 字段") from None


def create_settings_blueprint(config_root=None, *, read_only=False):
    """Register behind the host application's existing admin guard."""
    blueprint = Blueprint("control_settings", __name__)
    store = SettingsStore(config_root)

    @blueprint.before_request
    def protect_read_only_config():
        if read_only and request.method == "PUT":
            return jsonify(success=False, error="分身配置在管理页只读；请修改工作区的对应文件后重启分身"), 403

    def respond(action):
        try:
            return jsonify(success=True, result=action())
        except ConfigurationError as exc:
            return jsonify(success=False, error=str(exc)), exc.status
        except (OSError, UnicodeError):
            return jsonify(success=False, error="无法读写配置目录；请检查挂载与文件权限"), 503

    def body():
        if request.content_length and request.content_length > MAX_BYTES + 8192:
            raise ConfigurationError("请求过大")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ConfigurationError("请求须为 JSON 对象")
        return payload

    @blueprint.route("/api/control/settings", methods=["GET", "PUT"])
    def settings_endpoint():
        return respond(lambda: store.settings() if request.method == "GET" else store.update_settings(body()))

    @blueprint.route("/api/control/documents/<document_id>", methods=["GET", "PUT"])
    def document_endpoint(document_id):
        return respond(lambda: store.document(document_id) if request.method == "GET" else store.update_document(document_id, body()))

    return blueprint
