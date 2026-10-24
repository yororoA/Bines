"""Attach-only browser reads. This module never launches or navigates Chrome."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import build_opener, ProxyHandler
from urllib.parse import urlsplit


class ExistingBrowserUnavailable(RuntimeError):
    pass


class ExistingPageReader:
    def __init__(self, profile_root):
        self.profile_root = Path(profile_root)
        self.http = build_opener(ProxyHandler({}))
        self.ports = {}
        self.targets = {}

    def _json(self, url):
        with self.http.open(url, timeout=2) as response:
            return json.load(response)

    def _ports(self, platform):
        """Find only Chrome instances using this exact Bines profile."""
        profile = (self.profile_root / platform).resolve()
        candidates = []
        try:
            port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
            candidates.append(port)
        except (OSError, ValueError, IndexError):
            pass
        if os.name == "posix":
            rows = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                  text=True, timeout=3, check=False).stdout
            for row in rows.splitlines():
                fields = row.strip().split(None, 1)
                if len(fields) != 2:
                    continue
                pid, command = fields
                if "--type=" in command or not re.match(
                    r"^.+?(?:/Contents/MacOS/Google Chrome for Testing|/chrome)(?: --|$)", command,
                ):
                    continue
                match = re.search(r"--user-data-dir=(.+?)(?: --|$)", command)
                if not match or Path(match.group(1)).resolve() != profile:
                    continue
                output = subprocess.run(
                    ["lsof", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN", "-Fn"],
                    capture_output=True, text=True, timeout=3, check=False,
                ).stdout
                candidates.extend(int(port) for port in re.findall(r"^n(?:localhost|127\.0\.0\.1|\[::1\]):(\d+)$", output, re.M))
        return list(dict.fromkeys(port for port in candidates if 1 <= port <= 65535))

    def _pages(self, platform):
        if platform not in self.ports:
            for port in self._ports(platform):
                try:
                    version = self._json(f"http://127.0.0.1:{port}/json/version")
                    if "Chrome" in version.get("Browser", ""):
                        self.ports[platform] = port
                        break
                except (OSError, TypeError, ValueError):
                    continue
        if platform not in self.ports:
            raise ExistingBrowserUnavailable("没有可连接的 Bines 浏览器窗口；后台不会启动窗口")
        try:
            pages = self._json(f"http://127.0.0.1:{self.ports[platform]}/json/list")
            return [page for page in pages if page.get("type") == "page"
                    and page.get("url", "").startswith("https://") and page.get("webSocketDebuggerUrl")]
        except (OSError, TypeError, ValueError) as exc:
            self.ports.pop(platform, None)
            self.targets.pop(platform, None)
            raise ExistingBrowserUnavailable("Bines 浏览器连接已断开；后台观察已暂停") from exc

    @staticmethod
    def _call(target, method, params=None, *, timeout=5):
        import websocket
        endpoint = urlsplit(target["webSocketDebuggerUrl"])
        if endpoint.scheme != "ws" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ExistingBrowserUnavailable("浏览器调试地址不是本机连接")
        connection = None
        try:
            connection = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=timeout, suppress_origin=True,
                http_no_proxy=["localhost", "127.0.0.1", "::1"],
            )
            connection.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                message = json.loads(connection.recv())
                if message.get("id") != 1:
                    continue
                if message.get("error") or message.get("result", {}).get("exceptionDetails"):
                    raise ExistingBrowserUnavailable("浏览器只读观察返回错误")
                return message.get("result", {})
            raise ExistingBrowserUnavailable("浏览器只读观察超时")
        except (OSError, ValueError, websocket.WebSocketException) as exc:
            raise ExistingBrowserUnavailable("浏览器只读连接失败") from exc
        finally:
            if connection is not None:
                connection.close()

    def _target(self, config):
        from common.website import validate_url
        pages = self._pages(config.key)
        visible = []
        for page in pages:
            try:
                validate_url(config, page["url"])
                state = self._call(page, "Runtime.evaluate", {
                    "expression": "({visible:document.visibilityState==='visible',focused:document.hasFocus()})",
                    "returnByValue": True,
                })["result"].get("value", {})
                if state.get("visible"):
                    visible.append((page, state.get("focused", False)))
            except (ValueError, ExistingBrowserUnavailable):
                continue
        focused = [page for page, focus in visible if focus]
        if len(focused) == 1:
            selected = focused[0]
        elif len(visible) == 1:
            selected = visible[0][0]
        else:
            raise ExistingBrowserUnavailable("没有唯一可见的 Bines 页面；后台不会切换标签或创建页面")
        self.targets[config.key] = selected["id"]
        return selected

    def run(self, config, args, *, timeout=5):
        target = self._target(config)
        if args[0] == "eval" and len(args) == 2:
            result = self._call(target, "Runtime.evaluate", {
                "expression": args[1], "returnByValue": True, "awaitPromise": False,
            }, timeout=timeout)
            value = result["result"].get("value")
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if args[0] == "screenshot" and len(args) == 2:
            result = self._call(target, "Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": False, "fromSurface": True,
            }, timeout=timeout)
            Path(args[1]).write_bytes(base64.b64decode(result["data"]))
            return "captured"
        if args == ["tab", "list"]:
            return "visible_target=" + target["id"]
        raise ValueError("后台浏览器只允许读取状态或截图")
