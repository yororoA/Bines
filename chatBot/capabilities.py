"""One authority for HTTP, Skills and MCP. Dynamic schemas describe all NapCat actions."""
import hashlib
import json
import threading
import time
import base64
import re
from pathlib import PurePosixPath
from urllib.request import Request, build_opener, ProxyHandler
from chatBot.config import WEBUI_URL, WEBUI_TOKEN, ROOT
from chatBot.messages import (
    segments, outgoing_segments, SEND_ACTIONS, FORWARD_ACTIONS, sent_message,
    compact_qq_send_params,
)
from chatBot.catalog import reference_catalog, with_runtime_schema, bundled_document

# Safe conversation operations are explicit; new/unknown actions require local ownership.
SCOPED = {
    "get_group_info", "get_group_member_info", "get_group_member_list",
    "get_group_msg_history", "get_friend_msg_history", "get_msg",
    "send_group_msg", "send_private_msg", "send_msg", "delete_msg", "send_like",
    "send_group_forward_msg", "send_private_forward_msg", "send_forward_msg",
}
OWNER_GROUP_READS = {"fetch_custom_face", "fetch_custom_face_detail"}
IMAGE_URL_TTL_SECONDS = 15 * 60
CUSTOM_FACE_URL_PREFIX = "https://p.qpic.cn/qq_expression/"


def allowed_actions(identity, schemas):
    if identity.qq_admin:
        return set(schemas)
    return set(schemas) & SCOPED


def is_read(action):
    return action.startswith(("get_", "_get_", "fetch_", "check_", "can_", "ocr_")) or action in {"nc_get_packet_status"}


class SchemaCatalog:
    def __init__(self, url=WEBUI_URL, token=WEBUI_TOKEN):
        self.url, self.token = url.rstrip("/"), token
        self._schemas, self._expires = {}, 0
        self._runtime = None
        self._runtime_error = None
        self._lock = threading.Lock()
        self._opener = build_opener(ProxyHandler({}))

    def _request(self, path, payload=None, credential=None):
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        request = Request(self.url+path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with self._opener.open(request, timeout=10) as response:
            data = json.load(response)
        if data.get("code") != 0:
            raise RuntimeError("NapCat WebUI schema 认证或读取失败")
        return data.get("data")

    def schemas(self, refresh=False):
        with self._lock:
            if self._schemas and time.monotonic() < self._expires and not refresh:
                return self._schemas
            self._schemas = {name: dict(entry) for name, entry in reference_catalog().items()}
            self._runtime = None
            self._runtime_error = None
            if not self.token:
                self._runtime_error = "未配置 WebUI token，使用随项目附带的官方文档"
            else:
                try:
                    auth = self._request("/api/auth/login", {"hash": hashlib.sha256((self.token+".napcat").encode()).hexdigest()})
                    data = self._request("/api/Debug/schemas", credential=auth["Credential"])
                    if not isinstance(data, dict) or not all(isinstance(entry, dict) for entry in data.values()):
                        raise RuntimeError("NapCat schema 格式无效")
                    merged = {name: {**entry, "runtime_supported": name in data}
                              for name, entry in self._schemas.items()}
                    for name, entry in data.items():
                        merged[name] = with_runtime_schema(name, entry)
                    self._schemas = merged
                    self._runtime = set(data)
                except Exception:
                    # Do not silently erase the full API reference when WebUI is unavailable.
                    self._runtime_error = "WebUI schema 查询失败，运行时支持未确认；使用官方文档"
            self._expires = time.monotonic() + (300 if self._runtime is not None else 30)
            return self._schemas

    def status(self, identity):
        schemas = self.schemas()
        manifest, _ = bundled_document()
        documented = set(reference_catalog())
        allowed = allowed_actions(identity, schemas)
        return {
            "documented_version": manifest["version"], "source": manifest["source"],
            "documented_actions": len(documented), "catalog_actions": len(schemas),
            "allowed_actions": len(allowed), "all_actions_allowed": identity.local or identity.owner,
            "runtime_verified": self._runtime is not None,
            "runtime_actions": len(self._runtime) if self._runtime is not None else None,
            "missing_in_runtime": sorted(documented - self._runtime) if self._runtime is not None and (identity.local or identity.owner) else None,
            "runtime_only_actions": sorted(self._runtime - documented) if self._runtime is not None and (identity.local or identity.owner) else None,
            "notice": self._runtime_error,
        }

    def discover(self, identity, query="", action=None):
        schemas = self.schemas()
        allowed_names = allowed_actions(identity, schemas)
        allowed = {key: value for key, value in schemas.items() if key in allowed_names}
        if action:
            if action not in allowed:
                raise PermissionError("当前身份无权发现该动作")
            return {"action": action, **allowed[action]}
        return [{"action": key, "description": value.get("description", ""),
                 "tags": value.get("tags", []), "schema_source": value.get("schema_source"),
                 "runtime_supported": value.get("runtime_supported")}
                for key, value in allowed.items()
                if query.lower() in (key+" "+str(value.get("description", ""))).lower()]


class CapabilityService:
    def __init__(self, client, store, catalog=None):
        self.client, self.store = client, store
        self.catalog = catalog or SchemaCatalog()
        self.started_at = time.time()

    def _message(self, identity, message_id):
        # Equality matters: LIKE would let a model-supplied "%" bypass the scope lookup.
        message_id = str(message_id)
        if not re.fullmatch(r"[1-9]\d*", message_id):
            raise PermissionError("需使用当前会话中真实的 message_id")
        external_id = f"{identity.account}:{identity.group or 'p:'+identity.user}:{message_id}"
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM messages WHERE scope=? AND external_id=? AND recalled=0",
                (identity.scope, external_id),
            ).fetchone()
        if not row:
            raise PermissionError("不能引用或转发其他会话、不存在或已撤回的消息")
        return row

    def quoted_message(self, identity, event_id, message_id):
        """Resolve only a reply actually present in this event, checking its peer."""
        message_id = str(message_id)
        if not event_id or not re.fullmatch(r"[1-9]\d*", message_id):
            raise PermissionError("引用消息需来自当前 QQ 事件")
        with self.store.connection() as db:
            row = db.execute("SELECT raw FROM events WHERE id=?", (event_id,)).fetchone()
        event = json.loads(row["raw"]) if row else {}
        current = self.store.identity(event)
        references = {
            str((part.get("data") or {}).get("id"))
            for part in segments(event.get("message", event.get("raw_message", "")))
            if part.get("type") == "reply"
        }
        if current != identity or message_id not in references:
            raise PermissionError("该消息不是本轮实际引用的消息")
        external_id = f"{identity.account}:{identity.group or 'p:'+identity.user}:{message_id}"
        with self.store.connection() as db:
            cached = db.execute(
                "SELECT raw,recalled FROM messages WHERE scope=? AND external_id=?",
                (identity.scope, external_id),
            ).fetchone()
        if cached and cached["recalled"]:
            return {"status": "failed", "msg": "引用消息已撤回"}
        cached_data = None
        if cached and cached["raw"]:
            try:
                cached_data = json.loads(cached["raw"])
            except (TypeError, ValueError):
                cached_data = None
        cached_segments = segments(
            (cached_data or {}).get("message", (cached_data or {}).get("raw_message", ""))
        )
        expiring_media = any(
            part.get("type") in {"image", "record", "video", "file"}
            for part in cached_segments
        )
        if cached_data and not expiring_media:
            return {"status": "ok", "retcode": 0, "data": cached_data}
        # Refresh expiring image URLs through NapCat; cached text remains useful
        # when the original message is no longer available from the history API.
        result = self.client.raw_api("get_msg", {"message_id": message_id}, timeout=10)
        if not isinstance(result, dict) or result.get("status") != "ok":
            if cached_data:
                return {"status": "ok", "data": cached_data}
            return {"status": "failed", "msg": "引用消息暂不可读取"}
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise PermissionError("引用消息返回格式无效")
        same_message = str(data.get("message_id")) == message_id
        same_account = str(data.get("self_id", identity.account)) == identity.account
        if identity.group:
            same_peer = (data.get("message_type") == "group"
                         and str(data.get("group_id")) == identity.group)
        else:
            sender = str(data.get("user_id") or (data.get("sender") or {}).get("user_id") or "")
            outgoing_peer = str(data.get("target_id") or data.get("peer_id") or "")
            same_peer = (data.get("message_type") == "private" and not data.get("group_id")
                         and (sender == identity.user or
                              (sender == identity.account and
                               (outgoing_peer == identity.user or bool(cached)))))
        if not (same_message and same_account and same_peer):
            raise PermissionError("引用消息不属于当前会话")
        return result

    def _image_source(self, identity, source):
        if not isinstance(source, str) or not source:
            raise ValueError("image 需要 file")
        if source.startswith("base64://"):
            encoded = source.removeprefix("base64://")
            if len(encoded) > 3 * 1024 * 1024:
                raise ValueError("内联图片超过 3 MiB；请使用公开素材文件")
            try:
                binary = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise ValueError("图片 base64 无效") from None
            if not binary:
                raise ValueError("图片内容为空")
            return
        # Only an explicit public media directory can be used by ordinary QQ users.
        public = PurePosixPath("/bines-media/public")
        path = PurePosixPath(source)
        if path.is_relative_to(public) and ".." not in path.parts and "\\" not in source:
            host_root = ROOT / "deploy" / "napcat" / "data" / "media" / "public"
            host_path = host_root.joinpath(*path.relative_to(public).parts).resolve()
            if host_path.is_relative_to(host_root.resolve()) and host_path.is_file():
                return
            raise ValueError("公开图片文件不存在或超出素材目录")
        if identity.owner and source.startswith(CUSTOM_FACE_URL_PREFIX):
            result = self.client.raw_api("fetch_custom_face", {"count": 100}, timeout=30)
            available = result.get("data") if isinstance(result, dict) and result.get("status") == "ok" else []
            if source in (available or []):
                return
            raise ValueError("该账号收藏表情已失效或不在当前表情列表中")
        # Resend an image actually observed in this conversation. Text containing
        # a path/URL is not an image grant, nor is a file from another QQ conversation.
        valid_after = max(self.started_at, time.time() - IMAGE_URL_TTL_SECONDS)
        with self.store.connection() as db:
            rows = db.execute(
                """SELECT raw FROM messages
                WHERE scope=? AND recalled=0 AND raw IS NOT NULL AND ts>=?""",
                (identity.scope, valid_after),
            )
            for row in rows:
                event = json.loads(row["raw"])
                for part in segments(event.get("message", [])):
                    data = part.get("data") or {}
                    if part.get("type") == "image" and source in (data.get("file"), data.get("url")):
                        return
        raise PermissionError("图片需来自当前会话、base64 内容或 /bines-media/public 公开素材")

    def _send_segments(self, identity, chain):
        allowed_fields = {
            "text": {"text"}, "at": {"qq", "name"}, "reply": {"id"},
            "face": {"id", "resultId", "chainCount"},
            "image": {"file", "summary", "sub_type"},
            "mface": {"emoji_package_id", "emoji_id", "key", "summary"},
            "node": {"id"},
        }
        for part in chain:
            kind, data = part["type"], part["data"]
            if kind not in allowed_fields or set(data) - allowed_fields[kind]:
                raise PermissionError(f"当前会话不支持该 {kind} 消息段或附加字段")
            if kind == "at" and str(data.get("qq")) not in {identity.user, identity.account}:
                raise PermissionError("只能艾特当前用户或机器人")
            if kind in {"reply", "node"}:
                self._message(identity, data.get("id"))
            if kind == "image":
                self._image_source(identity, data.get("file"))
                if str(data.get("sub_type", 0)) not in {"0", "1"}:
                    raise ValueError("图片 sub_type 为 0（普通图片）或 1（表情）")
            if kind == "mface" and not all(data.get(key) for key in ("emoji_package_id", "emoji_id", "key")):
                raise ValueError("商城表情必须提供 emoji_package_id、emoji_id 和 key")

    def authorize(self, identity, action, params):
        if identity.qq_admin:
            return
        if action not in SCOPED:
            raise PermissionError("该 NapCat 动作需要本机或显式绑定的主人授权")
        if action.endswith("msg_history") and params.get("message_seq"):
            # NapCat resolves a short message ID to a global peer; a foreign cursor
            # must not pivot a conversation-scoped history request.
            self.authorize(identity, "get_msg", {"message_id": params["message_seq"]})
        if action in {"get_msg", "delete_msg"}:
            row = self._message(identity, params.get("message_id"))
            if action == "delete_msg" and row["role"] != "assistant":
                raise PermissionError("仅可查询当前会话记录或撤回机器人在此会话发送的消息")
            return
        if action == "send_like":
            if identity.group or str(params.get("user_id")) != identity.user:
                raise PermissionError("只能给当前私聊用户点赞")
            return
        generic_send = action in {"send_msg", "send_forward_msg"}
        group_action = action.startswith(("get_group_", "send_group_")) or (generic_send and bool(params.get("group_id")))
        private_action = action.startswith(("get_friend_", "send_private_")) or (generic_send and not params.get("group_id"))
        if group_action:
            valid = identity.group and str(params.get("group_id")) == identity.group
            if params.get("user_id") is not None and str(params["user_id"]) != identity.user:
                valid = False
        elif private_action:
            valid = not identity.group and str(params.get("user_id")) == identity.user and not params.get("group_id")
        else:
            valid = False
        if not valid:
            raise PermissionError("QQ 工具仅允许访问当前会话")
        if action in SEND_ACTIONS:
            expected_type = "group" if identity.group else "private"
            if params.get("message_type", expected_type) != expected_type or (identity.group and params.get("user_id")):
                raise PermissionError("message_type 与目标会话不一致")
            self._send_segments(identity, outgoing_segments(sent_message(params)))
        if action.endswith("msg_history"):
            count = int(params.get("count", 20))
            if not 1 <= count <= 100:
                raise ValueError("count 必须为 1–100")

    def prepare(self, identity, action, params=None):
        """Normalize and authorize an action without dispatching it."""
        if not isinstance(action, str) or not action or len(action) > 128 or not isinstance(params or {}, dict):
            raise ValueError("action 和 params 格式无效")
        params = dict(params or {})
        if action in SEND_ACTIONS and not (identity.local or identity.owner):
            if "message" in params and "messages" in params:
                raise ValueError("不能同时传 message 和 messages")
            field = "messages" if action in FORWARD_ACTIONS and "messages" in params else "message"
            if "messages" in params and action not in FORWARD_ACTIONS:
                raise ValueError("普通消息使用 message 字段")
            params[field] = outgoing_segments(params.get(field), params.get("auto_escape") in {True, "true"})
            if action in FORWARD_ACTIONS and any(part["type"] != "node" for part in params[field]):
                raise ValueError("合并转发需使用 node 消息段")
        self.authorize(identity, action, params)
        return compact_qq_send_params(action, params)

    def call(self, identity, action, params=None, operation_id=None, timeout=30):
        params = self.prepare(identity, action, params)
        if not is_read(action):
            if not operation_id:
                raise ValueError("写操作必须携带 operation_id")
            new, previous = self.store.begin_operation(operation_id, action, params)
            if not new:
                return previous
        result = self.client.raw_api(action, params, timeout=max(1, min(float(timeout), 120)))
        if isinstance(result, dict) and result.get("status") == "failed" and result.get("retcode") in {200, 1200}:
            # NapCat catches handler exceptions (including timeouts) under these
            # generic codes, after a write may already have affected QQ.
            if not is_read(action):
                result = {**result, "status": "uncertain"}
        if operation_id and not is_read(action):
            self.store.complete_operation(operation_id, result)
        return result
