"""Scope-bound sticker assets; no sending, model calls or source-URL persistence."""
import hashlib
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit
import warnings

from PIL import Image

MAX_BYTES = 12 * 1024 * 1024
DOWNLOAD_TIMEOUT = 12
MAX_FRAMES = 500
MAX_DECODED_PIXELS = 100_000_000
SYNC_STATES = frozenset({"local_only", "not_applicable", "pending", "confirmed", "uncertain", "failed"})
PUBLIC_FIELDS = ("id", "description", "tags", "type", "animated", "md5", "qq_sync", "qq_sync_detail")


def _public(meta):
    return {key: meta[key] for key in PUBLIC_FIELDS if key in meta}


def shared_media_dir():
    from chatBot.config import ROOT
    return Path(ROOT) / "deploy" / "napcat" / "data" / "media"


def _public_addresses(host, port, timeout):
    """Bound DNS time, then pin the connection to an inspected address."""
    result = queue.Queue(maxsize=1)

    def lookup():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:
            result.put(exc)

    threading.Thread(target=lookup, daemon=True).start()
    try:
        addresses = result.get(timeout=max(.01, timeout))
    except queue.Empty:
        raise TimeoutError("表情下载域名解析超时") from None
    if isinstance(addresses, Exception):
        raise ValueError("表情下载域名无法解析") from None
    ips = list(dict.fromkeys(item[4][0] for item in addresses))
    def allowed(ip):
        address = ipaddress.ip_address(ip)
        return (address.is_global and not address.is_multicast and not address.is_reserved
                and not address.is_unspecified and not getattr(address, "sixtofour", None)
                and not getattr(address, "teredo", None))

    if not ips or any(not allowed(ip) for ip in ips):
        raise ValueError("表情下载不允许私网或保留地址")
    return ips


def download_image(url, *, max_bytes=MAX_BYTES, timeout=DOWNLOAD_TIMEOUT):
    """No proxies; validate every redirect and use the checked IP, not a second DNS lookup."""
    deadline = time.monotonic() + timeout
    for _ in range(4):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "\\" in url or any(ord(c) < 33 for c in url)):
            raise ValueError("表情下载需要有效的 HTTP(S) 来源")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("表情下载超时")
        ips = _public_addresses(parsed.hostname, port, remaining)
        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining)
        timer, transport = None, {}
        expired = threading.Event()

        def abort():
            expired.set()
            active = transport.get("socket")
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                active.close()

        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("表情下载超时")
            sock = socket.create_connection((ips[0], port), timeout=remaining)
            conn.sock = sock
            transport["socket"] = sock
            if parsed.scheme == "https":
                conn.sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=parsed.hostname, do_handshake_on_connect=False)
                transport["socket"] = conn.sock
            timer = threading.Timer(max(.001, deadline - time.monotonic()), abort)
            timer.daemon = True
            timer.start()
            if parsed.scheme == "https":
                conn.sock.do_handshake()
            conn.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                         headers={"Accept": "image/*", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("表情下载重定向缺少目标")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("表情下载失败")
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > max_bytes):
                raise ValueError("表情图片超过大小限制")
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("表情下载超时")
                if conn.sock:
                    conn.sock.settimeout(remaining)
                chunk = response.read(min(65536, max_bytes + 1 - len(data)))
                if expired.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("表情下载超时")
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("表情图片超过大小限制")
        except (OSError, http.client.HTTPException):
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("表情下载超时") from None
            raise
        finally:
            if timer is not None:
                timer.cancel()
            conn.close()
    raise ValueError("表情下载重定向过多")


def _labels(description, tags):
    if not isinstance(description, str) or len(description.strip()) > 160:
        raise ValueError("表情描述最多 160 字")
    if not isinstance(tags, (list, tuple)) or len(tags) > 8:
        raise ValueError("表情标签最多 8 个")
    if any(not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 32 for tag in tags):
        raise ValueError("表情标签需要 1–32 字的文本")
    if any(re.search(r"(?:https?://|file://|base64://|data:)", value, re.I) for value in [description, *tags]):
        raise ValueError("描述和标签不包含链接或内联图片")
    return description.strip(), list(dict.fromkeys(tag.strip() for tag in tags))


def _image_info(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ValueError("表情图片必须是非空字节，且不超过 12 MiB")
    if data.startswith((b"GIF87a", b"GIF89a")):
        expected = "GIF"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        expected = "PNG"
    elif data.startswith(b"\xff\xd8\xff"):
        expected = "JPEG"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        expected = "WEBP"
    else:
        raise ValueError("不支持的表情图片签名")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                if picture.format != expected or picture.width * picture.height > 20_000_000:
                    raise ValueError("表情图片格式或尺寸无效")
                picture.verify()
            with Image.open(io.BytesIO(data)) as picture:
                frames, decoded_pixels = 0, 0
                while True:
                    pixels = picture.width * picture.height
                    frames += 1
                    decoded_pixels += pixels
                    if frames > MAX_FRAMES or pixels > 20_000_000 or decoded_pixels > MAX_DECODED_PIXELS:
                        raise ValueError("表情动画超过解码预算")
                    picture.load()
                    try:
                        picture.seek(frames)
                    except EOFError:
                        break
                animated = frames > 1
    except Exception as exc:
        raise ValueError("表情图片损坏或尺寸过大") from exc
    return {"GIF": "gif", "PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[expected], animated


class StickerLibrary:
    def __init__(self, store, media_dir=None, downloader=None):
        self.store = store
        self.media_dir = Path(media_dir or shared_media_dir()).resolve()
        self.directory = self.media_dir / "stickers"
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.resolve().is_relative_to(self.media_dir):
            raise ValueError("表情目录不能指向共享媒体目录之外")
        self.downloader = downloader or download_image
        with self.store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sticker_library (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, digest TEXT NOT NULL,
                metadata TEXT NOT NULL, segment TEXT NOT NULL, filename TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(scope,digest))""")

    @staticmethod
    def _scopes(identity):
        return (identity.scope, "global")

    def _row(self, identity, sticker_id, *, write=False):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM sticker_library WHERE id=? AND scope IN (?,?)",
                             (str(sticker_id), *self._scopes(identity))).fetchone()
        if row is None or (write and row["scope"] == "global" and not identity.privileged):
            raise PermissionError("表情不存在或不属于当前可访问范围")
        return dict(row)

    def _path(self, filename):
        if not re.fullmatch(r"[a-f0-9]{64}\.(gif|png|jpg|webp)", filename):
            raise ValueError("表情文件名无效")
        path = self.directory / filename
        if path.is_symlink() or not path.resolve().is_relative_to(self.media_dir):
            raise ValueError("表情文件超出共享媒体目录")
        return path

    def _write_image(self, filename, image_bytes, digest):
        path = self._path(filename)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("表情文件内容校验失败")
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(image_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, identity, segment, description, tags, source_message_id="", shared=False, image_bytes=None,
             expected_md5="", before_save=None):
        if type(shared) is not bool:
            raise ValueError("shared 必须是布尔值")
        description, tags = _labels(description, tags)
        if shared and not identity.privileged:
            raise PermissionError("只有主人私聊或本机可显式共享表情")
        scope = "global" if shared else identity.scope
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            raise ValueError("表情需要真实 OneBot 消息段")
        kind, data = segment.get("type"), segment["data"]
        filename, md5, animated = "", "", None
        if kind == "image":
            if image_bytes is None:
                source = data.get("url") or data.get("file")
                if isinstance(source, Path):
                    resolved = source.resolve(strict=True)
                    if source != resolved or not resolved.is_relative_to(self.media_dir) or not resolved.is_file():
                        raise PermissionError("本地图片必须是共享目录内已解析批准的文件")
                    with resolved.open("rb") as stream:
                        image_bytes = stream.read(MAX_BYTES + 1)
                elif isinstance(source, str) and urlsplit(source).scheme in {"http", "https"}:
                    image_bytes = self.downloader(source, max_bytes=MAX_BYTES, timeout=DOWNLOAD_TIMEOUT)
                else:
                    raise PermissionError("图片需要已授权下载来源或批准的图片字节")
            extension, animated = _image_info(image_bytes)
            digest = hashlib.sha256(image_bytes).hexdigest()
            md5 = hashlib.md5(image_bytes).hexdigest()
            if expected_md5 and (not isinstance(expected_md5, str) or md5 != expected_md5.lower()):
                raise ValueError("收藏表情内容与指定 MD5 不一致")
            if before_save is not None and before_save() is False:
                raise PermissionError("表情来源授权已失效")
            filename = digest + "." + extension
            saved = {"type": "image", "data": {"file": "/bines-media/stickers/" + filename, "sub_type": 1}}
        elif kind in {"face", "mface"}:
            keys = ("id", "resultId", "chainCount") if kind == "face" else ("emoji_package_id", "emoji_id", "key")
            required = keys[:1] if kind == "face" else keys
            if any(isinstance(data.get(key), bool) or not isinstance(data.get(key), (str, int))
                   or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", str(data[key])) for key in required):
                raise ValueError("原生表情缺少有效标识")
            clean = {key: str(data[key]) for key in keys if key in data}
            pattern = r"[0-9]{1,20}" if kind == "face" else r"[A-Za-z0-9_-]{1,256}"
            if any(not re.fullmatch(pattern, value) for value in clean.values()):
                raise ValueError("原生表情标识无效")
            saved = {"type": kind, "data": clean}
            digest = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
            if before_save is not None and before_save() is False:
                raise PermissionError("表情来源授权已失效")
        else:
            raise ValueError("只支持 image、face 和 mface 表情")
        sticker_id = "stk_" + hashlib.sha256((scope + "\0" + digest).encode()).hexdigest()[:24]
        now = time.time()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # The hook must only read the same store, never perform network or writes.
            # A source withdrawal writer cannot pass this check before our commit.
            if before_save is not None and before_save() is False:
                raise PermissionError("表情来源授权已失效")
            if filename:
                self._write_image(filename, image_bytes, digest)
            previous = db.execute("SELECT metadata FROM sticker_library WHERE id=?", (sticker_id,)).fetchone()
            meta = json.loads(previous["metadata"]) if previous else {}
            meta.update(id=sticker_id, description=description, tags=tags, type=kind, animated=animated, md5=md5)
            meta.setdefault("source_message_id", str(source_message_id)[:128])
            meta.setdefault("source_scope", identity.scope)
            meta.setdefault("qq_sync", "local_only" if kind == "image" else "not_applicable")
            db.execute("""INSERT INTO sticker_library VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,updated=excluded.updated""",
                (sticker_id, scope, digest, json.dumps(meta, ensure_ascii=False), json.dumps(saved), filename, now, now))
        return _public(meta)

    def metadata(self, identity, sticker_id):
        return _public(json.loads(self._row(identity, sticker_id)["metadata"]))

    def resolve(self, identity, sticker_id):
        row = self._row(identity, sticker_id)
        if row["filename"]:
            path = self._path(row["filename"])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["digest"]:
                raise ValueError("表情文件缺失或校验失败")
        return json.loads(row["segment"])

    def authorize_path(self, identity, source):
        if not isinstance(source, str) or not source.startswith("/bines-media/stickers/"):
            return False
        filename = source.removeprefix("/bines-media/stickers/")
        with self.store.connection() as db:
            rows = db.execute("SELECT id FROM sticker_library WHERE filename=? AND scope IN (?,?)",
                              (filename, *self._scopes(identity))).fetchall()
        for row in rows:
            try:
                if self.resolve(identity, row["id"])["data"]["file"] == source:
                    return True
            except (ValueError, OSError, PermissionError):
                pass
        return False

    def update(self, identity, sticker_id, description=None, tags=None):
        def change(meta):
            meta["description"], meta["tags"] = _labels(meta["description"] if description is None else description,
                                                       meta["tags"] if tags is None else tags)
        return self._mutate(identity, sticker_id, change)

    def update_sync(self, identity, sticker_id, state, detail=""):
        if state not in SYNC_STATES:
            raise ValueError("表情同步状态无效")
        # Upstream errors can contain signed download URLs; never persist them.
        detail = re.sub(r"(?:https?://|base64://|data:)[^\s]+", "[已隐去]", str(detail), flags=re.I)[:160]
        return self._mutate(identity, sticker_id, lambda meta: meta.update(qq_sync=state, qq_sync_detail=detail))

    def _mutate(self, identity, sticker_id, change):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT scope,metadata FROM sticker_library WHERE id=? AND scope IN (?,?)",
                             (str(sticker_id), *self._scopes(identity))).fetchone()
            if row is None or (row["scope"] == "global" and not identity.privileged):
                raise PermissionError("表情不存在或不属于当前可访问范围")
            meta = json.loads(row["metadata"])
            change(meta)
            db.execute("UPDATE sticker_library SET metadata=?,updated=? WHERE id=?",
                       (json.dumps(meta, ensure_ascii=False), time.time(), sticker_id))
        return _public(meta)

    def search(self, identity, query="", limit=5):
        if not isinstance(query, str) or len(query) > 160 or isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("检索需要最多 160 字查询与整数 limit")
        limit = max(0, min(5, limit))
        query = query.strip().casefold()
        terms = set(re.findall(r"\w+", query))
        for run in re.findall(r"[\u3400-\u9fff]+", query):
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
        with self.store.connection() as db:
            rows = db.execute("SELECT metadata,updated FROM sticker_library WHERE scope IN (?,?)", self._scopes(identity)).fetchall()
        ranked = []
        for row in rows:
            meta = json.loads(row["metadata"])
            text = (meta["description"] + " " + " ".join(meta["tags"])).casefold()
            score = 20 * bool(query and query in text) + sum(2 for term in terms if term in text)
            score += sum(8 for tag in meta["tags"] if query and tag.casefold() in query)
            if not query or score:
                ranked.append((score, row["updated"], meta["id"], meta))
        ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
        return [_public(row[3]) for row in ranked[:limit]]
