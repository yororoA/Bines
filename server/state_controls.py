"""Authenticated administrative commands using the existing domain interfaces.

The application must install common.admin_auth.install_admin_guard. Runtime
facts stay read-only; the routes expose only named, validated state operations.
"""
from __future__ import annotations

from contextlib import closing
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from flask import Blueprint, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 1.5
STATE_FIELDS = frozenset({
    "current_time", "current_location", "active_quest", "important_thing",
    "npc_name", "npc_attire", "npc_visual_status", "npc_activity",
    "add_item", "remove_item", "add_memory_highlight", "remove_memory_highlight",
    "relationship_delta",
})
RETRY_REASONS = {"connectivity_rechecked", "configuration_corrected", "request_corrected"}
JOB_KINDS = {"associate", "repair_memory", "summarize", "diary", "summarize_experience", "audit_memory"}


class ControlError(Exception):
    def __init__(self, message, code="validation", status=400):
        self.message, self.code, self.status = message, code, status


def _with_extensions(state):
    # Older memory stores can carry user-defined fields alongside the fields
    # managed by semantic operations. Preserve the latter through those APIs.
    managed = STATE_FIELDS | {"inventory", "memory_highlights", "npc_state", "relationship_score",
                              "relationship_level", "relationship_distribution"}
    return {**state, "extensions": {key: value for key, value in state["data"].items()
                                     if key not in managed and not key.startswith(("_", "npc_state."))}}


def _backend_error(exc, *, retry=False):
    # Inspect known error categories, but never return backend exception strings:
    # they may contain URLs, passwords, model replies or private provider data.
    text = str(exc)
    if type(exc).__name__ == "StateConflict" or "StateConflict" in text or (retry and any(phrase in text for phrase in (
            "no longer matches", "error changed", "terminal recovery guard", "UNIQUE constraint"))):
        return ControlError("状态已变化，请刷新后再操作", "conflict", 409)
    if "生命周期刚调整过" in text:
        return ControlError("生命周期仍在调整冷却期，请稍后再试", "conflict", 409)
    if "时长必须为" in text:
        return ControlError("调节时长超出当前配置允许范围，请检查生命节律设置")
    return ControlError("服务暂不可用，请检查连接与配置；操作结果不确定时请先刷新查看", "unavailable", 503)


def _endpoint(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            response = jsonify(success=True, result=function(*args, **kwargs))
            response.headers["Cache-Control"] = "no-store"
            return response
        except ControlError as exc:
            return jsonify(success=False, error=exc.message, code=exc.code), exc.status
        except Exception as exc:
            error = _backend_error(exc, retry=request.path.endswith("/retry"))
            return jsonify(success=False, error=error.message, code=error.code), error.status
    return wrapped


def _body(required, optional=()):
    if request.content_length and request.content_length > 32768:
        raise ControlError("请求内容过大")
    value = request.get_json(silent=True)
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ControlError("请求字段缺失或包含不支持的字段")
    if len(json.dumps(value, ensure_ascii=False).encode()) > 32768:
        raise ControlError("请求内容过大")
    return value


def _scope(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ControlError("记忆范围必须是 1 到 256 字符的非空文本")
    return value


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ControlError(f"{label}必须是 {low} 到 {high} 之间的整数")
    return value


def _string(value, low, high, label):
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise ControlError(f"{label}必须是 {low} 到 {high} 字符的文本")
    return value.strip()


def _choice(value, choices):
    return isinstance(value, str) and value in choices


def _gateway_call(method, **params):
    from chatBot.config import GATEWAY_URL, gateway_token
    req = Request(GATEWAY_URL.rstrip("/") + "/memory",
                  data=json.dumps({"method": method, "params": params}).encode(), headers={
                      "Content-Type": "application/json", "Authorization": "Bearer " + gateway_token(),
                  })
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=TIMEOUT) as response:
            result = json.load(response)
    except HTTPError as exc:
        # Gateway errors use transport status codes. Only known conflicts are
        # surfaced; raw error messages never leave this adapter.
        if exc.code == 409:
            raise ControlError("状态已变化，请刷新后再操作", "conflict", 409) from None
        body = exc.read(8192).decode("utf-8", errors="replace")
        raise _backend_error(RuntimeError(body), retry=method == "retry_failed_job") from None
    if not isinstance(result, dict) or "result" not in result:
        raise ControlError("记忆服务响应无效", "unavailable", 503)
    return result["result"]


def _memory_client():
    from config import ZMQ_HOST, ZMQ_PORTS
    source = str(ROOT / "thingking" / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from memory.client import MemoryClient, MemoryUnavailable

    class GatewayClient(MemoryClient):
        def __init__(self):
            pass

        def call(self, method, timeout_ms=None, **params):
            return _gateway_call(method, **params)

    backend = os.getenv("BINES_MEMORY_BACKEND", "auto").lower()
    if backend not in {"auto", "embedded", "remote"}:
        raise ControlError("记忆后端配置无效", "unavailable", 503)
    if backend == "embedded":
        return GatewayClient()
    client = MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"], timeout_ms=int(TIMEOUT * 1000))
    if backend == "auto" and ZMQ_HOST.strip().lower() in {"localhost", "127.0.0.1", "::1"}:
        try:
            client.call("ping")
        except (MemoryUnavailable, ConnectionError, TimeoutError):
            return GatewayClient()
    # Select the transport before sending commands. Never replay a timed-out
    # mutation against another backend: it might already have committed.
    return client


def _new_autonomy_store():
    from common.autonomy_store import AutonomyStore
    store = AutonomyStore(timeout=TIMEOUT, sqlite_timeout=TIMEOUT)
    if store.backend == "redis":
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        store.redis.connection_pool.connection_kwargs.update(retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
    return store


def _autonomy_control():
    """Read only: never seed legacy files or create a missing SQLite database."""
    from common.autonomy_store import memory_directory
    backend = os.getenv("BINES_AUTONOMY_BACKEND", "redis").lower()
    control, revision, configured = {}, 0, True
    if backend == "sqlite":
        path = Path(os.getenv("BINES_AUTONOMY_SQLITE_PATH") or memory_directory() / "autonomy.db")
        if not path.exists():
            configured = False
        else:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=TIMEOUT) as db:
                row = db.execute("SELECT value,revision FROM documents WHERE key='control'").fetchone()
                if row:
                    control, revision = json.loads(row[0]), row[1]
    elif backend == "redis":
        store = _new_autonomy_store()
        try:
            control, revision = store.get("control")
        finally:
            store.redis.close()
    else:
        raise ControlError("自主状态后端配置无效", "unavailable", 503)
    return {"mode": control.get("mode", "auto"), "reason": control.get("reason", ""),
            "updated_at": control.get("updated_at"), "revision": revision,
            "backend": backend, "configured": configured}


# Scheduler documents may also contain message excerpts, per-turn observations
# and contact IDs. Project only the fields needed by the operator at the store.
_SCHEDULER_NUMBERS = (
    "next_decision", "next_intention_at", "sent_today", "actions_today",
    "emotion.valence", "emotion.arousal", "emotion.social", "emotion.curiosity",
    "drives.connection", "drives.curiosity", "drives.initiative",
)
_SCHEDULER_CATEGORIES = {
    "activity.mode": ("continue", "pause", "review", "finish"),
    "pending.kind": ("connection", "curiosity", "initiative"),
}
_SCHEDULER_COUNTS = ("intention_queue", "waiting_activities")
_SCHEDULER_PAGE = 16


def _scheduler_item(key, revision, values):
    channel = "desktop" if key == "desktop" else "qq"
    identity = "desktop" if channel == "desktop" else hashlib.sha256(key.encode()).hexdigest()[:12]
    item = {"id": identity, "channel": channel, "revision": int(revision), "emotion": {}, "drives": {}}
    for name in _SCHEDULER_NUMBERS:
        value = values.get(name)
        if type(value) in (int, float) and math.isfinite(value):
            group, dot, field = name.partition(".")
            if dot:
                item[group][field] = value
            else:
                item[name] = value
    for name, choices in _SCHEDULER_CATEGORIES.items():
        value = values.get(name)
        item[name.replace(".", "_")] = value if value in choices else None
    item["intention_count"] = values.get("intention_queue", 0)
    item["waiting_count"] = values.get("waiting_activities", 0)
    return item


def _sqlite_schedulers(cursor):
    from common.autonomy_store import memory_directory
    from chatBot.config import GATEWAY_DIR
    directory = memory_directory()
    # The QQ store has a separate SQLite path even when desktop/control are
    # configured to use a custom path. Both use the same namespace on Redis.
    desktop = Path(os.getenv("BINES_AUTONOMY_SQLITE_PATH") or directory / "autonomy.db")
    qq = GATEWAY_DIR / "gateway.autonomy.db"
    fields = (*_SCHEDULER_NUMBERS, *_SCHEDULER_CATEGORIES)
    columns = [f"CASE WHEN json_type(value, '$.{f}') IN ('integer','real') THEN json_extract(value, '$.{f}') END"
               for f in _SCHEDULER_NUMBERS]
    for name, choices in _SCHEDULER_CATEGORIES.items():
        # All SQL expressions below are constructed solely from this module's allowlist.
        allowed = ", ".join(repr(choice) for choice in choices)
        value = f"json_extract(value, '$.{name}')"
        columns.append(f"CASE WHEN {value} IN ({allowed}) THEN {value} END")
    columns.extend(f"(SELECT COUNT(*) FROM json_each(documents.value, '$.{f}'))" for f in _SCHEDULER_COUNTS)
    select = "SELECT key, revision, " + ", ".join(columns) + " FROM documents WHERE "
    items = []
    next_cursor = None
    sources = [(qq, "key GLOB 'qq:*'", cursor)]
    if not cursor:
        sources.insert(0, (desktop, "key='desktop'", 0))
    for path, condition, offset in sources:
        if not path.exists():
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=TIMEOUT)) as db:
            rows = db.execute(select + condition + " ORDER BY key LIMIT ? OFFSET ?",
                              (_SCHEDULER_PAGE + 1, offset)).fetchall()
        if len(rows) > _SCHEDULER_PAGE:
            next_cursor = str(offset + _SCHEDULER_PAGE)
        for key, revision, *data in rows[:_SCHEDULER_PAGE]:
            items.append(_scheduler_item(key, revision, dict(zip((*fields, *_SCHEDULER_COUNTS), data))))
    return {"items": items, "next_cursor": next_cursor}


_REDIS_SCHEDULER = """
local row = redis.call('HMGET', KEYS[1], 'value', 'revision')
if not row[1] then return false end
local state = cjson.decode(row[1])
local fields = cjson.decode(ARGV[1])
local output = {}
for _, name in ipairs(fields.numeric) do
    local value = state
    for part in string.gmatch(name, '[^.]+') do
        value = type(value) == 'table' and value[part] or nil
    end
    if type(value) == 'number' then output[name] = value end
end
for name, choices in pairs(fields.category) do
    local group, field = string.match(name, '([^.]+)%.(.+)')
    local value = type(state[group]) == 'table' and state[group][field]
    for _, choice in ipairs(choices) do
        if value == choice then output[name] = value end
    end
end
for _, name in ipairs(fields.counts) do
    local count = 0
    if type(state[name]) == 'table' then
        for _ in pairs(state[name]) do count = count + 1 end
    end
    output[name] = count
end
return {row[2], cjson.encode(output)}
"""


def _redis_schedulers(cursor):
    store = _new_autonomy_store()
    try:
        # Escape Redis glob metacharacters in custom namespace prefixes.
        prefix_pattern = re.sub(r"([*?\[\]\\])", r"\\\1", store.prefix)
        next_cursor, keys = store.redis.scan(cursor, match=prefix_pattern + "qq:*", count=_SCHEDULER_PAGE)
        if not cursor:
            keys.insert(0, store.prefix + "desktop")
        spec = json.dumps({"numeric": _SCHEDULER_NUMBERS, "category": _SCHEDULER_CATEGORIES,
                           "counts": _SCHEDULER_COUNTS})
        with store.redis.pipeline(transaction=False) as pipeline:
            for key in keys:
                pipeline.eval(_REDIS_SCHEDULER, 1, key, spec)
            rows = pipeline.execute()
        items = [_scheduler_item(key[len(store.prefix):], row[0], json.loads(row[1]))
                 for key, row in zip(keys, rows) if row]
        return {"items": items, "next_cursor": str(next_cursor) if next_cursor else None}
    finally:
        store.redis.close()


def _component(callback):
    try:
        return {"available": True, "data": callback()}
    except Exception as exc:
        error = exc if isinstance(exc, ControlError) else _backend_error(exc)
        return {"available": False, "error": error.message, "code": error.code}


def create_state_controls_blueprint(supervisor=None, host_provider=None):
    """Register after the application's existing admin guard.

    host_provider may return presence/game_mode snapshots from module_manager;
    no host status is inferred inside a standalone/Docker RAG Web process.
    """
    blueprint = Blueprint("state_controls", __name__)

    @blueprint.get("/api/control/runtime")
    @_endpoint
    def runtime():
        from config import HTTP_PORTS
        scope = _scope(request.args.get("scope", "owner"))
        scopes = {"owner", scope}
        try:
            client = _memory_client()
        except Exception as exc:
            error = _backend_error(exc)
            state = life = {"available": False, "error": error.message, "code": error.code}
            scope_status = {"available": False, "error": error.message}
        else:
            state = _component(lambda: _with_extensions(client.get_state(scope)))
            life = _component(lambda: client.life_snapshot(scope))
            stats = _component(client.stats)
            scope_status = {"available": stats["available"]}
            if stats["available"]:
                data = stats["data"]
                scopes.update(value for value in data.get("state_scopes", []) if isinstance(value, str))
                for key in ("memories", "messages"):
                    scopes.update(data.get(key, {}).keys())
            else:
                scope_status["error"] = stats["error"]
        autonomy = _component(_autonomy_control)
        host = {"available": supervisor is not None,
                "url": f"http://127.0.0.1:{HTTP_PORTS['MODULE_MANAGER']}/"}
        if supervisor is None:
            host["error"] = "当前入口不管理宿主进程，请打开宿主管理页"
        if supervisor is not None and host_provider:
            host["runtime"] = _component(host_provider)
        return {"scope": scope, "scopes": sorted(scopes), "scope_status": scope_status,
                "state": state, "life": life, "autonomy": autonomy, "host": host,
                "capabilities": {"memory_state": state["available"],
                    "life": life["available"] and scope == "owner", "autonomy": autonomy["available"],
                    **{key: supervisor is not None for key in (
                        "host_services", "presence", "game_mode", "skill_actions", "realtime_screen")}}}

    @blueprint.get("/api/control/schedulers")
    @_endpoint
    def schedulers():
        raw = request.args.get("cursor", "0")
        if not re.fullmatch(r"[0-9]{1,20}", raw):
            raise ControlError("分页游标无效")
        cursor = int(raw)
        backend = os.getenv("BINES_AUTONOMY_BACKEND", "redis").lower()
        if backend == "sqlite" and cursor < 2 ** 63:
            return _sqlite_schedulers(cursor)
        if backend == "redis" and cursor < 2 ** 64:
            return _redis_schedulers(cursor)
        raise ControlError("分页游标或自主调度存储配置无效")

    @blueprint.put("/api/control/state")
    @_endpoint
    def update_state():
        data = _body({"scope", "expected_version", "changes"})
        scope = _scope(data["scope"])
        version = _integer(data["expected_version"], 0, 2 ** 53 - 1, "状态版本")
        changes = data["changes"]
        if not isinstance(changes, dict) or not changes or set(changes) - STATE_FIELDS:
            raise ControlError("请选择支持的状态操作")
        for key, value in changes.items():
            if key == "relationship_delta":
                _integer(value, -10000, 10000, "好感度增量")
            elif not isinstance(value, str) or len(value) > 4000:
                raise ControlError("状态操作必须为不超过 4000 字符的文本")
        return _with_extensions(_memory_client().put_state(scope, {}, expected_version=version, changes=changes))

    @blueprint.put("/api/control/state/extensions")
    @_endpoint
    def update_extensions():
        body = _body({"scope", "expected_version", "extensions"})
        scope = _scope(body["scope"])
        version = _integer(body["expected_version"], 0, 2 ** 53 - 1, "状态版本")
        client = _memory_client()
        current = _with_extensions(client.get_state(scope))
        if current["version"] != version:
            raise ControlError("状态已变化，请刷新后再操作", "conflict", 409)
        values, previous = body["extensions"], current["extensions"]
        if not isinstance(values, dict) or not values or values.keys() != previous.keys():
            raise ControlError("只能更新当前作用域已有的扩展字段")
        if any(type(value) != type(previous[key]) for key, value in values.items()):
            raise ControlError("扩展字段须保留原有 JSON 类型")
        try:
            json.dumps(values, allow_nan=False)
        except ValueError:
            raise ControlError("扩展字段包含无效数值") from None
        saved = client.put_state(scope, {**current["data"], **values}, expected_version=version)
        return _with_extensions(saved)

    @blueprint.post("/api/control/life")
    @_endpoint
    def regulate_life():
        data = _body({"action", "reason", "operation_id"}, {"scope", "duration_minutes"})
        scope = _scope(data.get("scope", "owner"))
        if scope != "owner" or not _choice(data["action"], {"rest", "wake", "auto"}):
            raise ControlError("生命节律仅支持主人范围的 rest、wake 或 auto 操作")
        operation_id = data["operation_id"]
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", operation_id):
            raise ControlError("操作编号必须是稳定的 8 到 128 字符标识")
        reason = _string(data["reason"], 2, 300, "调节原因")
        duration = _integer(data.get("duration_minutes", 0), 0, 1440, "调节分钟数")
        if data["action"] != "auto" and duration < 5:
            raise ControlError("休息或清醒时长至少为 5 分钟")
        return _memory_client().regulate_life(scope, data["action"], duration, reason,
                                              "admin-control:" + operation_id)

    @blueprint.post("/api/control/autonomy")
    @_endpoint
    def control_autonomy():
        data = _body({"mode"}, {"reason"})
        if not _choice(data["mode"], {"pause", "resume", "stop"}):
            raise ControlError("自主活动仅支持 pause、resume 或 stop 操作")
        reason = _string(data.get("reason", ""), 0, 500, "原因")
        from common.activity import set_owner_control
        store = _new_autonomy_store()
        try:
            return set_owner_control(data["mode"], reason, store=store)
        finally:
            if store.backend == "redis":
                store.redis.close()

    @blueprint.get("/api/control/jobs")
    @_endpoint
    def jobs():
        status = request.args.get("status", "")
        if status not in {"", "pending", "running", "done", "failed", "cancelled"}:
            raise ControlError("任务状态筛选无效")
        try:
            offset, limit = int(request.args.get("offset", 0)), int(request.args.get("limit", 30))
        except ValueError:
            raise ControlError("分页参数必须是整数") from None
        _integer(offset, 0, 2 ** 31 - 1, "偏移量")
        _integer(limit, 1, 100, "每页条数")
        result = _memory_client().list_jobs(status=status, offset=offset, limit=limit)
        for item in result["items"]:
            error_type = item.get("error_type") or (item.get("last_error") or "").split(":", 1)[0]
            valid_type = isinstance(error_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type)
            item["error_type"] = error_type if valid_type else None
            item["retryable"] = bool(item.get("status") == "failed" and item.get("kind") in JOB_KINDS
                                      and valid_type and "terminal_reason" in item and item["terminal_reason"] is None)
            # Raw model/provider exceptions can include credentials. Preserve
            # classification, not arbitrary provider response bodies.
            item["last_error"] = item["error_type"] or ("错误详情请查看受保护的服务日志" if item.get("last_error") else None)
        return result

    @blueprint.post("/api/control/jobs/<int:job_id>/retry")
    @_endpoint
    def retry_job(job_id):
        data = _body({"scope", "kind", "expected_attempt", "expected_updated_at", "expected_error_type", "reason"})
        _integer(job_id, 1, 2 ** 53 - 1, "任务编号")
        _scope(data["scope"])
        _integer(data["expected_attempt"], 0, 2 ** 31 - 1, "任务尝试版本")
        stamp = data["expected_updated_at"]
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            raise ControlError("任务更新时间无效")
        error_type = data["expected_error_type"]
        if (not _choice(data["kind"], JOB_KINDS) or not _choice(data["reason"], RETRY_REASONS)
                or not isinstance(error_type, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type)):
            raise ControlError("任务类型、错误类型或重试原因无效")
        return _memory_client().retry_failed_job(job_id, **data)

    @blueprint.post("/api/control/index/rebuild")
    @_endpoint
    def rebuild_index():
        _body(set())
        # The normal persistent server queues vector maintenance; use a bounded
        # RPC call even for an older server that performs a synchronous rebuild.
        return _memory_client().call("rebuild_index", timeout_ms=int(TIMEOUT * 1000))

    return blueprint
