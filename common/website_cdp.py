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
    def __init__(self, message, *, stage="connection"):
        super().__init__(message)
        self.stage = stage


class VideoPixelsUnavailable(ValueError):
    pass


class ExistingPageReader:
    def __init__(self, profile_root):
        self.profile_root = Path(profile_root)
        self.http = build_opener(ProxyHandler({}))
        self.ports = {}
        self.targets = {}
        # None means an explicit selection could not be synchronized. Do not
        # silently read another tab while the control session targets this one.
        self.bound_targets = {}

    def _json(self, url):
        with self.http.open(url, timeout=2) as response:
            return json.load(response)

    def _profile_pids(self, platform):
        """A leftover DevToolsActivePort file is not evidence of a live browser."""
        profile = (self.profile_root / platform).resolve()
        try:
            if os.name == "posix":
                result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                        text=True, timeout=3, check=False)
                rows = [line.strip().split(None, 1) for line in result.stdout.splitlines()]
            else:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                     "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                data = json.loads(result.stdout or "[]")
                rows = [[str(item["ProcessId"]), item.get("CommandLine") or ""]
                        for item in (data if isinstance(data, list) else [data])]
            if result.returncode:
                raise OSError("process discovery failed")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            raise ExistingBrowserUnavailable("无法确认 Bines 浏览器进程；暂停启动以免重复打开窗口",
                                             stage="process_discovery") from exc
        pids = []
        for row in rows:
            if len(row) != 2:
                continue
            pid, command = row
            if "--type=" in command or not re.match(
                r'^.+?(?:/Contents/MacOS/Google Chrome for Testing|/chrome|\\chrome\.exe)"?(?: --|$)',
                command, re.I,
            ):
                continue
            match = re.search(r'--user-data-dir=(?:"([^"]+)"|(.+?)(?= --|$))', command)
            if match and Path(match.group(1) or match.group(2)).resolve() == profile:
                pids.append(pid)
        return pids

    def _ports(self, platform):
        """Find only Chrome instances using this exact Bines profile."""
        profile = (self.profile_root / platform).resolve()
        pids = self._profile_pids(platform)
        if not pids:
            return []
        candidates = []
        try:
            port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
            candidates.append(port)
        except (OSError, ValueError, IndexError):
            pass
        if os.name == "posix":
            for pid in pids:
                try:
                    output = subprocess.run(
                        ["lsof", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN", "-Fn"],
                        capture_output=True, text=True, timeout=3, check=False,
                    ).stdout
                except (OSError, subprocess.SubprocessError):
                    continue
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
                connection.settimeout(max(0.05, deadline - time.monotonic()))
                message = json.loads(connection.recv())
                if message.get("id") != 1:
                    continue
                if message.get("error") or message.get("result", {}).get("exceptionDetails"):
                    raise ExistingBrowserUnavailable("浏览器只读观察返回错误：" + method, stage=method)
                return message.get("result", {})
            raise ExistingBrowserUnavailable("浏览器只读观察超时：" + method, stage=method)
        except (OSError, ValueError, websocket.WebSocketException) as exc:
            raise ExistingBrowserUnavailable("浏览器只读连接失败：" + method, stage=method) from exc
        finally:
            if connection is not None:
                connection.close()

    def bind(self, config, identity, pages):
        """Bind the control session's document without activating any window."""
        from common.website import validate_url
        self.bound_targets[config.key] = None
        self.targets.pop(config.key, None)
        validate_url(config, identity["url"])
        matches = []
        deadline = time.monotonic() + 3
        for page in pages:
            if page["url"] != identity["url"]:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExistingBrowserUnavailable("浏览器页面绑定超时", stage="target_binding")
            state = self._call(page, "Runtime.evaluate", {
                "expression": "({url:location.href,document_id:performance.timeOrigin})",
                "returnByValue": True,
            }, timeout=min(2, remaining))["result"].get("value", {})
            if state == identity:
                matches.append(page)
        if len(matches) != 1:
            raise ExistingBrowserUnavailable("无法确认 Bines 当前操作的页面；观察已暂停", stage="target_binding")
        self.bound_targets[config.key] = matches[0]["id"]

    def _target(self, config):
        from common.website import validate_url
        pages = self._pages(config.key)
        if config.key in self.bound_targets:
            selected = next((page for page in pages
                             if page["id"] == self.bound_targets[config.key]), None)
            if selected is None:
                self.targets.pop(config.key, None)
                raise ExistingBrowserUnavailable("Bines 操作的标签页已关闭或尚未绑定；观察已暂停",
                                                 stage="target_selection")
            try:
                validate_url(config, selected["url"])
            except ValueError as exc:
                self.targets.pop(config.key, None)
                raise ExistingBrowserUnavailable("Bines 操作的页面已离开允许域名；观察已暂停",
                                                 stage="target_selection") from exc
        else:
            allowed = []
            for page in pages:
                try:
                    validate_url(config, page["url"])
                    allowed.append(page)
                except ValueError:
                    continue
            if len(allowed) != 1:
                raise ExistingBrowserUnavailable("没有唯一已绑定的 Bines 页面；请先选择操作标签",
                                                 stage="target_selection")
            selected = allowed[0]
            self.bound_targets[config.key] = selected["id"]
        # A hidden/minimized document is still the same observable target.
        self.targets[config.key] = selected["id"]
        return selected

    def capture_video(self, config, index, path, *, timeout=4):
        """Read decoded video pixels without asking Chrome for a compositor frame."""
        target = self._target(config)
        script = """(()=>{
            const v=document.querySelectorAll('video')[INDEX];
            if(!v||v.readyState<2||!v.videoWidth||!v.videoHeight)
                return {error:'video_not_ready'};
            const r=v.getBoundingClientRect(),s=getComputedStyle(v);
            if(r.width<=0||r.height<=0||s.display==='none'||s.visibility==='hidden')
                return {error:'video_not_visible'};
            const fit=s.objectFit==='cover'?Math.max:Math.min;
            const scale=fit(r.width/v.videoWidth,r.height/v.videoHeight);
            const dw=v.videoWidth*scale,dh=v.videoHeight*scale;
            const left=r.left+(r.width-dw)/2,top=r.top+(r.height-dh)/2;
            const x=Math.max(0,r.left,left),y=Math.max(0,r.top,top);
            const right=Math.min(innerWidth,r.right,left+dw),bottom=Math.min(innerHeight,r.bottom,top+dh);
            if(right-x<32||bottom-y<32)return {error:'video_not_visible'};
            const c=document.createElement('canvas'),size=Math.min(1,1280/(right-x),1280/(bottom-y));
            c.width=Math.round((right-x)*size);c.height=Math.round((bottom-y)*size);
            try{
                c.getContext('2d').drawImage(v,(x-left)/scale,(y-top)/scale,
                    (right-x)/scale,(bottom-y)/scale,0,0,c.width,c.height);
                return {data:c.toDataURL('image/jpeg',0.82),media_time:v.currentTime};
            }catch(e){return {error:e.name==='SecurityError'?'cross_origin_video':'video_pixels_unavailable'};}
        })()""".replace("INDEX", str(int(index)))
        result = self._call(target, "Runtime.evaluate", {
            "expression": script, "returnByValue": True, "awaitPromise": False,
        }, timeout=timeout)["result"].get("value", {})
        if not isinstance(result, dict) or result.get("error") or not result.get("data", "").startswith("data:image/jpeg;base64,"):
            raise VideoPixelsUnavailable("当前视频像素无法直接读取")
        Path(path).write_bytes(base64.b64decode(result["data"].partition(",")[2], validate=True))
        return result.get("media_time")

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
                "format": "png", "captureBeyondViewport": False, "fromSurface": False,
            }, timeout=timeout)
            Path(args[1]).write_bytes(base64.b64decode(result["data"]))
            return "captured"
        if args == ["tab", "list"]:
            return "observed_target=" + target["id"]
        raise ValueError("后台浏览器只允许读取状态或截图")
