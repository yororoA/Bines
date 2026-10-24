"""Shared dependency/readiness/lifecycle coordinator for CLI and Web manager."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlsplit
import uuid

from common.env import env_path, read_env
from process_runtime import ProcessTree, build_python_runtime_env, find_port_listeners, resolve_process_command


class ServiceSupervisor:
    def __init__(self, definitions, *, emit=None, napcat=True):
        self.definitions = {row["name"]: dict(row) for row in definitions}
        self.records = {}
        self.lock = threading.RLock()
        self.lifecycle = threading.RLock()
        self.cancel = threading.Event()
        self.runtime = tempfile.TemporaryDirectory(prefix="bines-runtime-")
        self.emit = emit or (lambda name, state, message: None)
        self.use_napcat = napcat
        self._docker_owned = False
        self._docker_prepared = False
        self._napcat_message = "尚未检查"
        self._startup_thread = None
        self._startup_results = []
        self._startup_active = False
        self._http = build_opener(ProxyHandler({}))
        self._runtime_settings = {}
        self._closed = False

    def _state(self, name, state, message, **extra):
        with self.lock:
            record = self.records.setdefault(name, {"logs": deque(maxlen=2000)})
            record.update(state=state, message=message, **extra)
        self.emit(name, state, message)

    def plan(self, targets=None):
        targets = targets if targets is not None else [
            name for name, row in self.definitions.items() if row.get("autostart", True)
        ]
        result, seen, visiting = [], set(), set()
        def visit(name):
            if name in seen:
                return
            if name not in self.definitions:
                raise ValueError(f"未注册服务 {name}")
            if name in visiting:
                raise ValueError(f"服务依赖成环：{name}")
            visiting.add(name)
            for dependency in self.definitions[name].get("depends_on", []):
                visit(dependency)
            visiting.remove(name)
            seen.add(name)
            result.append(name)
        for name in targets:
            visit(name)
        return result

    def _read_log(self, name, pipe, label):
        try:
            for line in pipe:
                with self.lock:
                    self.records[name]["logs"].append(f"[{label}] {line.rstrip()}")
        finally:
            pipe.close()

    def _ready(self, name):
        row = self.definitions[name]
        record = self.records[name]
        if row.get("readiness") == "http":
            try:
                with self._http.open(row["ready_url"], timeout=1) as response:
                    return 200 <= response.status < 300
            except Exception:
                return False
        path = Path(self.runtime.name) / f"{name}.json"
        try:
            marker = json.loads(path.read_text())
            return marker.get("token") == record["token"]
        except (OSError, ValueError):
            return False

    def _start(self, name):
        row = self.definitions[name]
        existing = self.records.get(name, {}).get("process")
        if existing and existing.poll() is None and self.status(name)["state"] == "ready":
            return {"name": name, "success": True, "message": "已就绪", "already_running": True}
        if self.cancel.is_set():
            return {"name": name, "success": False, "message": "启动已取消"}
        if existing:
            self._stop(name)
        self._state(name, "starting", "正在启动")
        try:
            ports = [port for port in row.get("ports", []) if port]
            busy = find_port_listeners(ports)
            if busy:
                raise RuntimeError("端口已被占用：" + ", ".join(f"{port} (PID {pid})" for port, pid in busy.items()))
            command = resolve_process_command(row)
            token = uuid.uuid4().hex
            runtime_env = build_python_runtime_env()
            # Modules started from their own directories still import root packages.
            root = str(Path(__file__).resolve().parent)
            runtime_env["PYTHONPATH"] = root + (os.pathsep + runtime_env["PYTHONPATH"] if runtime_env.get("PYTHONPATH") else "")
            runtime_env.update(self._runtime_settings)
            runtime_env.update(read_env(env_path()))
            if name in {"Thinking", "QQ Thinking", "Bored Detector"} and not runtime_env.get("DEEPSEEK_API_KEY"):
                raise RuntimeError("请先在 .env 配置 DEEPSEEK_API_KEY")
            runtime_env.update({
                "_BINES_RUNTIME_DIR": self.runtime.name,
                "_BINES_SERVICE_NAME": name,
                "_BINES_SERVICE_TOKEN": token,
            })
            tree = ProcessTree(command, cwd=row["cwd"], env=runtime_env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            with self.lock:
                self.records[name].update(process=tree, token=token, start_time=time.time())
            for pipe, label in ((tree.process.stdout, "OUT"), (tree.process.stderr, "ERR")):
                threading.Thread(target=self._read_log, args=(name, pipe, label), daemon=True).start()
            deadline = time.monotonic() + float(os.environ.get("BINES_STARTUP_TIMEOUT", row.get("ready_timeout", 120)))
            while time.monotonic() < deadline and not self.cancel.is_set():
                exit_code = tree.poll()
                if exit_code is not None:
                    raise RuntimeError(f"进程退出（代码 {exit_code}）；" + " ".join(self.logs(name)[-3:]))
                if self._ready(name):
                    self._state(name, "ready", "已就绪")
                    return {"name": name, "success": True, "message": "已就绪", "pid": tree.pid}
                self.cancel.wait(0.1)
            raise RuntimeError("启动已取消" if self.cancel.is_set() else "等待就绪超时，请查看日志")
        except Exception as exc:
            self._stop(name)
            message = str(exc)
            self._state(name, "failed", message)
            return {"name": name, "success": False, "message": message}

    def _run_setup_command(self, command, label, timeout):
        """Run a bounded setup command while keeping noisy pull output in a runtime log."""
        log_path = Path(self.runtime.name) / "napcat-pull.log"
        with log_path.open("w", encoding="utf-8") as output:
            tree = ProcessTree(
                command, cwd=Path(__file__).resolve().parent,
                env=build_python_runtime_env(), stdout=output,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
            started = time.monotonic()
            next_notice = started + 30
            try:
                while tree.poll() is None:
                    now = time.monotonic()
                    if self.cancel.is_set():
                        raise RuntimeError("启动已取消")
                    if now - started > timeout:
                        raise RuntimeError(f"{label}超时（{int(timeout)} 秒）")
                    if now >= next_notice:
                        self._napcat_message = f"{label}（已等待 {int(now-started)} 秒）"
                        self.emit("NapCat", "starting", self._napcat_message)
                        next_notice = now + 30
                    self.cancel.wait(0.5)
                if tree.poll():
                    output.flush()
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    raise RuntimeError(f"{label}失败：" + " ".join(lines[-8:])[-800:])
            except BaseException:
                if tree.poll() is None:
                    tree.terminate()
                    try:
                        tree.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        tree.kill()
                        tree.wait()
                raise
            finally:
                tree.close()

    def _prepare_napcat(self):
        if not self.use_napcat:
            return
        from config import NAPCAT_WS_URL
        target = urlsplit(NAPCAT_WS_URL)
        mode = os.environ.get("_BINES_NAPCAT_DEPLOY") or os.environ.get("BINES_NAPCAT_DEPLOY", "auto")
        if mode not in {"auto", "external"}:
            raise ValueError("BINES_NAPCAT_DEPLOY 必须为 auto 或 external")
        local = target.hostname in {"127.0.0.1", "localhost", "::1"}
        port = target.port or (443 if target.scheme == "wss" else 80)
        available = False
        try:
            with socket.create_connection((target.hostname, port), timeout=1):
                available = True
        except OSError:
            pass
        if mode == "external" or not local or available:
            self._napcat_message = "连接外部 NapCat" if available else "等待外部 NapCat 上线"
            self.emit("NapCat", "ready" if available else "waiting", self._napcat_message)
            return
        if self._docker_prepared:
            return
        import napcat
        self._napcat_message = "正在启动 Docker NapCat"
        self.emit("NapCat", "starting", self._napcat_message)
        docker = napcat.ensure_docker_ready(timeout=180, cancel=self.cancel)
        command = [docker, "compose", "--project-directory", str(napcat.DEPLOY),
                   "-f", str(napcat.DEPLOY / "compose.yaml")]
        if self.cancel.is_set():
            raise RuntimeError("启动已取消")
        napcat.initialize(settings_path=env_path())
        self._runtime_settings.update({
            key: value for key, value in read_env(env_path()).items()
            if key.startswith("BINES_NAPCAT_")
        })
        prior = subprocess.run(command+["ps", "--status", "running", "-q", "napcat"],
                               capture_output=True, text=True, timeout=10)
        if not napcat.image_available(docker):
            value = os.environ.get("BINES_NAPCAT_PULL_TIMEOUT", "1800")
            try:
                pull_timeout = float(value)
            except ValueError as exc:
                raise ValueError("BINES_NAPCAT_PULL_TIMEOUT 必须为有限正数") from exc
            import math
            if not math.isfinite(pull_timeout) or pull_timeout <= 0:
                raise ValueError("BINES_NAPCAT_PULL_TIMEOUT 必须为有限正数")
            self._napcat_message = "首次运行正在下载 NapCat 镜像（可能需要数分钟）"
            self.emit("NapCat", "starting", self._napcat_message)
            self._run_setup_command(
                command+["pull", "napcat"], "正在下载 NapCat 镜像", pull_timeout,
            )
        startup = subprocess.run(
            command+["up", "-d", "--pull", "never"],
            capture_output=True, text=True, timeout=120,
        )
        if startup.returncode:
            raise RuntimeError("Docker NapCat 启动失败：" + startup.stderr[-600:])
        self._docker_owned = prior.returncode == 0 and not prior.stdout.strip()
        self._docker_prepared = True
        self._napcat_message = (
            "容器已启动，等待 QQ 登录；另开终端运行 python3 napcat.py logs 扫描二维码"
        )
        self.emit("NapCat", "waiting", self._napcat_message)

    def start(self, targets=None):
        with self.lifecycle:
            if not self._startup_active:
                self.cancel.clear()
            order = self.plan(targets)
            timeout = os.environ.get("BINES_STARTUP_TIMEOUT")
            if timeout is not None:
                import math
                value = float(timeout)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("BINES_STARTUP_TIMEOUT 必须为有限正数")
            finished = {name: threading.Event() for name in order}
            outcomes = {}
            def run_service(name):
                try:
                    if self.definitions[name].get("depends_on") and not self.status(name)["running"]:
                        self._state(name, "waiting", "等待前置服务就绪")
                    for dependency in self.definitions[name].get("depends_on", []):
                        while not finished[dependency].wait(0.1):
                            if self.cancel.is_set():
                                break
                    unavailable = [dep for dep in self.definitions[name].get("depends_on", [])
                                   if self.status(dep)["state"] != "ready"]
                    if unavailable:
                        message = "依赖未就绪：" + ", ".join(unavailable)
                        self._state(name, "blocked", message)
                        outcomes[name] = {"name": name, "success": False, "message": message}
                    else:
                        if name == "ChatBot":
                            current = self.status(name)
                            if not current["running"] or current["state"] != "ready":
                                self._state(name, "starting", "正在检查 NapCat 部署")
                                self._prepare_napcat()
                        outcomes[name] = self._start(name)
                except Exception as exc:
                    if name == "ChatBot":
                        self._napcat_message = str(exc)
                    self._state(name, "failed", str(exc))
                    outcomes[name] = {"name": name, "success": False, "message": str(exc)}
                finally:
                    finished[name].set()
            threads = [threading.Thread(target=run_service, args=(name,), daemon=True) for name in order]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            return [outcomes[name] for name in order]

    def start_background(self, targets=None):
        with self.lock:
            if self._startup_active or self._closed:
                return False
            self.plan(targets)  # Validate before accepting an asynchronous task.
            self.cancel.clear()
            self._startup_active = True
            self._startup_results = []
        def run():
            try:
                results = self.start(targets)
                with self.lock:
                    self._startup_results = results
            except Exception as exc:
                with self.lock:
                    self._startup_results = [{"name": "Bines", "success": False, "message": str(exc)}]
            finally:
                with self.lock:
                    self._startup_active = False
        self._startup_thread = threading.Thread(target=run, name="BinesStartup", daemon=True)
        self._startup_thread.start()
        return True

    def _stop(self, name):
        tree = self.records.get(name, {}).get("process")
        if tree:
            tree.terminate()
            try:
                tree.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tree.kill()
                tree.wait(timeout=5)
            tree.kill()  # Also clean up descendants whose launcher already exited.
            tree.close()
            with self.lock:
                self.records[name].pop("process", None)
        self._state(name, "stopped", "已停止")
        return {"name": name, "success": True, "message": "已停止"}

    def stop(self, name=None):
        self.cancel.set()
        with self.lifecycle:
            selected = set(self.definitions) if name is None else {name}
            if name is not None:
                changed = True
                while changed:
                    size = len(selected)
                    selected.update(key for key, row in self.definitions.items()
                                    if selected.intersection(row.get("depends_on", [])))
                    changed = len(selected) != size
            results = [self._stop(key) for key in reversed(self.plan(list(self.definitions)))
                       if key in selected and key in self.records]
            if name is None and self._docker_owned:
                import napcat
                docker = napcat.docker_executable()
                try:
                    if not docker:
                        raise OSError("Docker CLI unavailable")
                    command = [docker, "compose", "--project-directory", str(napcat.DEPLOY),
                               "-f", str(napcat.DEPLOY / "compose.yaml"), "stop", "napcat"]
                    outcome = subprocess.run(command, capture_output=True, timeout=30)
                    if outcome.returncode:
                        results.append({"name": "NapCat", "success": False, "message": "容器停止失败，请检查 Docker"})
                    else:
                        self._docker_owned = self._docker_prepared = False
                except (OSError, subprocess.SubprocessError):
                    results.append({"name": "NapCat", "success": False, "message": "Docker 不可用，无法停止本次启动的容器"})
            return results

    def status(self, name):
        with self.lock:
            record = self.records.get(name, {})
            tree = record.get("process")
            running = tree is not None and tree.poll() is None
            state = record.get("state", "stopped")
            message = record.get("message", "未启动")
            if tree and not running and state not in {"failed", "stopped"}:
                state, message = "failed", f"进程已退出（代码 {tree.poll()}）"
            if running and state == "ready":
                down = [dep for dep in self.definitions[name].get("depends_on", [])
                        if not self.status(dep)["running"] or self.status(dep)["state"] not in {"ready"}]
                if down:
                    state, message = "blocked", "运行中依赖退出：" + ", ".join(down)
            result = {"running": running, "pid": tree.pid if running else None, "state": state,
                      "message": message, "start_time": record.get("start_time")}
        if name == "ChatBot" and running and state == "ready":
            try:
                from chatBot.config import GATEWAY_URL, gateway_token
                token = self._runtime_settings.get("BINES_QQ_GATEWAY_TOKEN") or gateway_token()
                req = Request(GATEWAY_URL+"/health", data=b"{}",
                              headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
                with self._http.open(req, timeout=1) as response:
                    connected = json.load(response)["result"]["connected"]
                result["connected"] = connected
                result["message"] = "网关已就绪 · NapCat 已连接" if connected else "网关已就绪 · 等待 NapCat/QQ 登录"
            except Exception:
                result["message"] = "网关进程运行中，健康检查暂不可用"
        return result

    def logs(self, name, limit=1000):
        with self.lock:
            return list(self.records.get(name, {}).get("logs", []))[-limit:]

    def startup_status(self):
        with self.lock:
            return {"active": self._startup_active, "results": list(self._startup_results),
                    "napcat": self._napcat_message}

    def close(self):
        self._closed = True
        try:
            self.stop()
        finally:
            self.runtime.cleanup()
