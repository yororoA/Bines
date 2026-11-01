"""Compact live BuildKit progress with full output retained in a local log."""
from collections import deque
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from process_runtime import ProcessTree


class BuildProgress:
    def __init__(self, *, emit=print, clock=time.monotonic, service_hint="镜像"):
        self.emit, self.clock = emit, clock
        self.service_hint = service_hint
        self.started = self.clock()
        self.active = {}
        self.completed = set()
        self.last_notice = self.started

    def notice(self, text):
        seconds = int(self.clock() - self.started)
        self.emit(f"  [{seconds // 60:02d}:{seconds % 60:02d}] {text}", flush=True)
        self.last_notice = self.clock()

    def consume(self, line):
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
        match = re.match(r"#(\d+)\s+\[([^]]+)\]\s+(.+)", line)
        if match:
            key, scope, command = match.groups()
            service = scope.split()[0]
            if re.fullmatch(r"\d+/\d+", service):
                service = self.service_hint
            step = re.search(r"\b(\d+/\d+)\b", scope)
            label = f"（{step.group(1)}）" if step else ""
            if "pip install" in command:
                activity = "安装 Python 依赖"
            elif "apt-get" in command or command.startswith("RUN python /usr/local/bin/bines-install-apt.py"):
                activity = "安装系统依赖"
            elif command.startswith("COPY"):
                activity = "复制源码与配置"
            elif "useradd" in command:
                activity = "配置运行用户"
            elif "load build context" in command:
                activity = "读取构建文件"
            elif "load metadata" in command:
                activity = "读取基础镜像"
            else:
                activity = command[:100]
            text = f"{service} · {activity}{label}"
            if self.active.get(key) != text:
                self.active[key] = text
                self.notice(text)
            return
        match = re.match(r"#(\d+)\s+(?:[\d.]+\s+)?Downloading\s+(\S+).*?\(([^)]+)\)", line)
        if match:
            key, filename, size = match.groups()
            name = filename.rsplit("/", 1)[-1].split("-")[0][:60]
            service = self.active.get(key, "镜像").split(" · ")[0]
            self.active[key] = f"{service} · 下载 {name}（{size}）"
            large_download = bool(re.match(r"(?:[5-9]\d|\d{3,})(?:\.\d+)? MB|[\d.]+ GB", size))
            if large_download or self.clock() - self.last_notice >= 1:
                self.notice(self.active[key])
            return
        match = re.match(r"#(\d+)\s+(DONE|CACHED|ERROR)\b", line)
        if match:
            key, result = match.groups()
            text = self.active.pop(key, "")
            if result == "ERROR":
                self.notice((text or "构建") + " · 失败，请查看构建日志")
            else:
                self.completed.add(key)
            return
        if line.startswith("#") and any(text in line for text in (
            "exporting to", "sending tarball", "importing to docker", "unpacking to",
        )):
            self.notice("导出并加载镜像")

    def heartbeat(self):
        if self.clock() - self.last_notice >= 10:
            steps = "；".join(dict.fromkeys(self.active.values())) or "等待构建器返回"
            self.notice(f"{steps} · 已完成 {len(self.completed)} 个步骤")


def stream_build(command, *, cwd, env, log_path, timeout=3600, service_hint="镜像"):
    """Stream summaries now; never buffer the entire build until completion."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    progress = BuildProgress(service_hint=service_hint)
    progress.notice("构建开始 · 详细日志：" + str(log_path))
    tail = deque(maxlen=80)
    events = queue.Queue(maxsize=1024)
    cancelled = threading.Event()
    with log_path.open("w", encoding="utf-8") as log:
        tree = ProcessTree(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           text=True, encoding="utf-8", errors="replace", bufsize=1)
        process = tree.process

        def publish(value):
            while not cancelled.is_set():
                try:
                    events.put(value, timeout=.1)
                    return
                except queue.Full:
                    pass

        def read_output():
            try:
                for line in process.stdout:
                    if cancelled.is_set():
                        break
                    log.write(line)
                    log.flush()
                    publish(line)
            finally:
                publish(None)

        reader = threading.Thread(target=read_output, name="DockerBuildOutput", daemon=True)
        reader.start()
        try:
            ended = False
            while not ended or process.poll() is None:
                if time.monotonic() - progress.started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    line = events.get(timeout=.2)
                except queue.Empty:
                    progress.heartbeat()
                    continue
                if line is None:
                    ended = True
                else:
                    tail.append(line)
                    progress.consume(line)
                progress.heartbeat()
            process.wait()
            reader.join(timeout=2)
        except BaseException:
            cancelled.set()
            tree.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tree.kill()
                process.wait()
            reader.join(timeout=2)
            raise
        finally:
            tree.close()
            if process.stdout:
                process.stdout.close()
        progress.notice("构建完成" if process.returncode == 0 else "构建失败 · 日志：" + str(log_path))
        return subprocess.CompletedProcess(command, process.returncode, stdout="".join(tail), stderr="")
