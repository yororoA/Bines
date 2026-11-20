"""Bounded local client for the vendored See Now RGB24 pipeline.

The owning screen worker controls capture, inference and child lifetime. This module
never opens a screen, sends a network request or writes screenshots to disk.
"""
import base64
from collections import deque
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "integrations" / "see_now" / "bridge.mjs"
MAX_OUTPUT_BYTES = 24 * 1024 * 1024
MAX_QUEUE = 4

# Desktop UI changes often occupy a small part of a pale, otherwise static page.
# Do not apply the upstream film foreground/camera-motion filters to menus or text.
# These are capture choices, not a promise of lower model cost or complete OCR.
DESKTOP_OPTIONS = {
    "cutAbs": 1, "cutSpike": 1.2, "cutMcAbs": 0, "cutBgAbs": 0,
    "cutFutureDelay": 0, "cutIsolatedAbs": 0, "dupMin": 0.5,
    "integralSeconds": 0.5, "strategy": "full", "maxWaitSec": 10,
    "silentFloorSec": 20, "maxFrames": 9, "layout": "grid3", "queueDepth": 1, "audioStrip": False,
}

VIDEO_OPTIONS = dict(DESKTOP_OPTIONS)


class SeeNowError(RuntimeError):
    """The local vision preprocessor is unavailable or its protocol failed."""


def _version(path):
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                                timeout=3, check=False, creationflags=_creation_flags())
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
        return tuple(map(int, match.groups())) if result.returncode == 0 and match else None
    except (OSError, subprocess.SubprocessError):
        return None


def _creation_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def find_node_binary(explicit=None):
    """Honor an explicit binary; otherwise search installed Node versions, never install."""
    explicit = explicit or os.environ.get("BINES_NODE_BINARY")
    if explicit:
        path = shutil.which(str(explicit)) or str(Path(explicit).expanduser())
        version = _version(path)
        if not version or version[0] < 20:
            raise SeeNowError("BINES_NODE_BINARY must point to an executable Node.js >=20")
        return str(Path(path).resolve())
    home = Path.home()
    bundled = ROOT / ".tools/agent-browser/node_modules/node/bin" / ("node.exe" if os.name == "nt" else "node")
    candidates = [shutil.which("node"), bundled,
                  "/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"]
    nvm = Path(os.environ.get("NVM_DIR", str(home / ".nvm"))).expanduser()
    candidates += sorted(nvm.glob("versions/node/*/bin/node"), reverse=True)
    candidates += [home / ".volta/bin/node"]
    if os.name == "nt":
        for key in ("NVM_SYMLINK", "ProgramFiles", "LOCALAPPDATA"):
            if os.environ.get(key):
                root = Path(os.environ[key])
                candidates += [root / "node.exe", root / "nodejs/node.exe"]
        if os.environ.get("NVM_HOME"):
            candidates += sorted(Path(os.environ["NVM_HOME"]).glob("v*/node.exe"), reverse=True)
    seen = set()
    for item in candidates:
        if not item or str(item) in seen or not Path(item).is_file():
            continue
        seen.add(str(item))
        version = _version(item)
        if version and version[0] >= 20:
            return str(Path(item).resolve())
    raise SeeNowError("See Now requires Node.js >=20; install a supported Node or set BINES_NODE_BINARY")


class SeeNowClient:
    def __init__(self, width, height, fps, options=None, *, node_binary=None, timeout=5.0):
        if (isinstance(width, bool) or not isinstance(width, int) or not 64 <= width <= 2048
                or isinstance(height, bool) or not isinstance(height, int) or not 36 <= height <= 2048):
            raise ValueError("See Now dimensions must be integers in 64..2048 by 36..2048")
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 0.2 <= fps <= 30:
            raise ValueError("See Now fps must be between 0.2 and 30")
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 60:
            raise ValueError("See Now timeout must be between 0.1 and 60 seconds")
        self.width, self.height, self.fps = width, height, fps
        self.options = dict(options or {})
        self.node_binary, self.timeout = node_binary, timeout
        self._lifecycle_lock = threading.RLock()
        self._condition = threading.Condition()
        self._queue = deque()
        self._requests = {}
        self._next_id = 0
        self._latest = None
        self._process = None
        self._threads = []
        self._closed = False
        self._error = None
        self._stderr_tail = b""
        self._first_tick = None
        self._last_t = -1.0
        self._submitted = self._dropped = self._received = 0

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def start(self):
        with self._lifecycle_lock:
            return self._start()

    def _start(self):
        if self._closed:
            raise SeeNowError("See Now client is closed")
        if self._process is not None:
            self._check()
            return self
        if not BRIDGE_PATH.is_file():
            raise SeeNowError("See Now bridge is missing from integrations/see_now/bridge.mjs")
        self.node_binary = find_node_binary(self.node_binary)
        try:
            self._process = subprocess.Popen(
                [self.node_binary, str(BRIDGE_PATH)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                cwd=str(ROOT), creationflags=_creation_flags(),
            )
            for target, name in ((self._write_loop, "writer"), (self._read_loop, "reader"),
                                 (self._stderr_loop, "stderr")):
                thread = threading.Thread(target=target, name=f"see-now-{name}", daemon=True)
                self._threads.append(thread)
                thread.start()
            self._request("init", width=self.width, height=self.height, fps=self.fps, options=self.options)
            return self
        except Exception:
            self.close()
            raise

    def _check(self):
        if self._closed:
            raise SeeNowError("See Now client is closed")
        if self._error:
            raise SeeNowError(self._error)
        if self._process is None:
            raise SeeNowError("See Now client has not started")
        code = self._process.poll()
        if code is not None:
            raise SeeNowError(f"See Now process exited (code {code})")

    def _fail(self, message):
        with self._condition:
            if self._closed:
                return
            self._error = self._error or str(message)[:240]
            for request in self._requests.values():
                request["error"] = self._error
                request["event"].set()
            self._condition.notify_all()
        process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _frame_payload(self, image_or_rgb_bytes):
        if hasattr(image_or_rgb_bytes, "convert"):
            image = image_or_rgb_bytes.convert("RGB")
            if image.size != (self.width, self.height):
                image = image.resize((self.width, self.height))
            payload = image.tobytes()
        elif isinstance(image_or_rgb_bytes, (bytes, bytearray, memoryview)):
            payload = bytes(image_or_rgb_bytes)
        else:
            raise TypeError("frame must be a PIL image or RGB24 bytes")
        if len(payload) != self.width * self.height * 3:
            raise ValueError("RGB24 byte length does not match configured frame dimensions")
        return payload

    def push(self, image_or_rgb_bytes, captured_at=None, *, sample_time=None):
        """Queue a live frame, keeping newer input when capture outruns processing.

        Use process() for finite files: it waits for acknowledgement without dropping
        queued frames. Do not mix live push() and synchronous process() in one client.
        captured_at is wall-clock metadata; algorithm time is monotonic/sample_time.
        """
        self._check()
        payload = self._frame_payload(image_or_rgb_bytes)
        stamp = time.time() if captured_at is None else float(captured_at)
        if not math.isfinite(stamp) or stamp < 0:
            raise ValueError("captured_at must be finite epoch seconds")
        with self._condition:
            self._check()
            tick = time.monotonic()
            if self._first_tick is None:
                self._first_tick = tick
            relative = tick - self._first_tick if sample_time is None else float(sample_time)
            if not math.isfinite(relative) or relative < 0 or relative <= self._last_t:
                raise ValueError("sample_time must be finite, nonnegative and strictly increasing")
            if len(self._queue) >= MAX_QUEUE:
                # A queued flush is a barrier: later capture must not discard its input.
                barrier = max((index for index, (header, _) in enumerate(self._queue)
                               if header["op"] != "frame"), default=-1)
                if barrier + 1 >= len(self._queue):
                    self._dropped += 1
                    return False
                del self._queue[barrier + 1]
                self._dropped += 1
            self._last_t = relative
            self._queue.append(({"op": "frame", "t": relative, "captured_at": stamp}, payload))
            self._submitted += 1
            self._condition.notify_all()
        return True

    def process(self, image_or_rgb_bytes, *, sample_time, captured_at=None):
        """Process one finite-source frame and return every emitted sheet, losslessly."""
        self._check()
        payload = self._frame_payload(image_or_rgb_bytes)
        relative = float(sample_time)
        stamp = time.time() if captured_at is None else float(captured_at)
        if not math.isfinite(relative) or relative < 0 or relative <= self._last_t:
            raise ValueError("sample_time must be finite, nonnegative and strictly increasing")
        if not math.isfinite(stamp) or stamp < 0:
            raise ValueError("captured_at must be finite epoch seconds")
        self._last_t = relative
        result = self._request("frame_sync", _payload=payload, t=relative, captured_at=stamp)
        self._submitted += 1
        self._received += len(result["sheets"])
        return result["sheets"]

    def finish(self):
        """Drain a finite source, without duplicating the last already-emitted frame."""
        result = self._request("finish")
        self._received += len(result["sheets"])
        return result["sheets"]

    def take(self):
        """Return and consume the newest available sheet, or None."""
        with self._condition:
            self._check()
            sheet, self._latest = self._latest, None
            return sheet

    def flush(self, timeout=None):
        """Synchronously produce a sheet containing the most recently queued frame."""
        result = self._request("flush", timeout=timeout)
        sheet = result.get("sheet")
        with self._condition:
            if sheet and self._latest and self._latest["sequence"] <= sheet["sequence"]:
                self._latest = None
        return sheet

    def _request(self, op, timeout=None, _payload=b"", **fields):
        timeout = self.timeout if timeout is None else float(timeout)
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 60:
            raise ValueError("request timeout must be between 0.1 and 60 seconds")
        deadline = time.monotonic() + timeout
        with self._condition:
            self._check()
            while len(self._queue) >= MAX_QUEUE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SeeNowError("See Now command queue timed out")
                self._condition.wait(remaining)
                self._check()
            self._next_id += 1
            request_id = self._next_id
            request = {"event": threading.Event()}
            self._requests[request_id] = request
            self._queue.append(({"op": op, "id": request_id, **fields}, _payload))
            self._condition.notify_all()
        if not request["event"].wait(max(0, deadline - time.monotonic())):
            self._fail("See Now bridge response timed out")
        with self._condition:
            self._requests.pop(request_id, None)
        if request.get("error"):
            raise SeeNowError(request["error"])
        if "result" not in request:
            raise SeeNowError("See Now request was interrupted")
        return request["result"]

    def _write_loop(self):
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._closed and not self._error:
                        self._condition.wait()
                    if self._closed or self._error:
                        return
                    header, payload = self._queue.popleft()
                    self._condition.notify_all()
                header = json.dumps({**header, "payload_bytes": len(payload)},
                                    separators=(",", ":"), allow_nan=False).encode()
                if len(header) > 8192:
                    raise SeeNowError("See Now command header exceeds size limit")
                for part in (struct.pack(">I", len(header)), header, payload):
                    view = memoryview(part)
                    while view:
                        written = self._process.stdin.write(view)
                        if not written:
                            raise SeeNowError("See Now input pipe closed")
                        view = view[written:]
        except (OSError, ValueError, TypeError, SeeNowError):
            self._fail("See Now input pipe failed")

    @staticmethod
    def _decode_sheet(sheet):
        if sheet is None:
            return None
        if not isinstance(sheet, dict):
            raise ValueError("invalid sheet")
        encoded = sheet.pop("png_base64")
        if not isinstance(encoded, str) or len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError("invalid PNG size")
        png = base64.b64decode(encoded, validate=True)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid PNG")
        sheet["png"] = png
        return sheet

    def _read_loop(self):
        reader = None
        try:
            reader = io.BufferedReader(self._process.stdout, buffer_size=64 * 1024)
            while True:
                line = reader.readline(MAX_OUTPUT_BYTES + 1)
                if not line:
                    self._fail("See Now process closed its output")
                    return
                if len(line) > MAX_OUTPUT_BYTES or not line.endswith(b"\n"):
                    raise ValueError("output exceeds limit")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid message")
                if message.get("event") == "error":
                    self._fail("See Now bridge: " + str(message.get("message", "failed"))[:160])
                    return
                if "sheet" in message:
                    message["sheet"] = self._decode_sheet(message["sheet"])
                if "sheets" in message:
                    if not isinstance(message["sheets"], list) or len(message["sheets"]) > 16:
                        raise ValueError("invalid sheet batch")
                    message["sheets"] = [self._decode_sheet(sheet) for sheet in message["sheets"]]
                with self._condition:
                    if message.get("event") == "sheet":
                        self._latest = message["sheet"]
                        self._received += 1
                    elif message.get("event") in ("ready", "response"):
                        request = self._requests.get(message.get("id"))
                        if request:
                            request["result"] = message
                            request["event"].set()
                    else:
                        raise ValueError("unexpected event")
        except (OSError, ValueError, KeyError, TypeError):
            self._fail("See Now output protocol failed")
        finally:
            if reader:
                reader.close()

    def _stderr_loop(self):
        try:
            while True:
                data = self._process.stderr.read(1024)
                if not data:
                    return
                self._stderr_tail = (self._stderr_tail + data)[-2048:]
        except (OSError, ValueError):
            pass

    def health(self):
        process = self._process
        return {"running": bool(process and process.poll() is None and not self._closed and not self._error),
                "pid": process.pid if process else None, "node_binary": self.node_binary,
                "error": self._error, "submitted": self._submitted, "dropped": self._dropped,
                "sheets": self._received}

    def close(self):
        with self._lifecycle_lock:
            self._close()

    def _close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            self._latest = None
            for request in self._requests.values():
                request["error"] = "See Now client is closed"
                request["event"].set()
            self._condition.notify_all()
        process = self._process
        if process:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                except OSError:
                    pass
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
