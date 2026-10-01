import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Union


def resolve_process_command(config: Dict[str, str]) -> List[str]:
    """从进程配置解析可执行命令。"""
    if "command" in config and config["command"]:
        cmd = config["command"]
        return cmd if isinstance(cmd, list) else [str(cmd)]
    return [config["interpreter"], config["script"]]


def build_python_runtime_env(base_env: Dict[str, str] = None) -> Dict[str, str]:
    """构造统一的 Python 子进程环境（UTF-8 输出）。"""
    env = dict(base_env or os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # Web 管理器用 PIPE 收集子进程日志；无缓冲输出可让启动异常立即显示。
    env["PYTHONUNBUFFERED"] = "1"
    # Include Python stack traces for native faults in child processes.
    env["PYTHONFAULTHANDLER"] = "1"
    if sys.platform == "win32":
        env["PYTHONUTF8"] = "1"
    return env


def find_port_listeners(ports) -> Dict[int, int]:
    """返回 {端口: 监听进程 PID}，只包含当前已被监听的端口。"""
    wanted = {int(p) for p in ports}
    if not wanted:
        return {}
    if sys.platform == "win32":
        cmd = ["netstat", "-ano", "-p", "TCP"]
    else:
        cmd = ["netstat", "-ltnp"]
    try:
        output = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    listeners = {}
    for line in output.splitlines():
        if "LISTEN" not in line.upper():
            continue
        match = re.search(r":(\d+)\s+\S+\s+LISTEN\w*\s+(\d+)", line, re.IGNORECASE)
        if match and int(match.group(1)) in wanted:
            listeners.setdefault(int(match.group(1)), int(match.group(2)))
    return listeners


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _CREATE_SUSPENDED = 0x4

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)


class ProcessTree:
    """启动一个子进程，并保证它及其派生的所有进程能被整体结束。

    Windows 上 venv 的 python.exe 只是启动器，真正的解释器是它的子进程；
    单独 terminate 启动器会留下占着端口的孤儿进程。这里把整棵进程树放进
    一个 KILL_ON_JOB_CLOSE 的 Job Object：调用 kill() 或管理器进程以任何
    方式退出（包括关掉窗口、崩溃）时，系统都会结束树里的全部进程。
    """

    def __init__(self, cmd, **popen_kwargs):
        self._job = None
        if sys.platform != "win32":
            popen_kwargs.setdefault("start_new_session", True)
            self.process = subprocess.Popen(cmd, **popen_kwargs)
            return

        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(job)
            raise ctypes.WinError(error)

        # Suspended start: the launcher must join the job before it can spawn the real interpreter.
        popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | _CREATE_SUSPENDED
        try:
            self.process = subprocess.Popen(cmd, **popen_kwargs)
        except Exception:
            _kernel32.CloseHandle(job)
            raise
        handle = int(self.process._handle)
        if not _kernel32.AssignProcessToJobObject(job, handle):
            error = ctypes.get_last_error()
            self.process.kill()
            _kernel32.CloseHandle(job)
            raise ctypes.WinError(error)
        self._job = job
        _ntdll.NtResumeProcess(handle)

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> Optional[int]:
        return self.process.poll()

    def wait(self, timeout: Union[float, None] = None) -> int:
        return self.process.wait(timeout=timeout)

    def terminate(self) -> None:
        # Windows has no graceful signal for windowless children; TerminateProcess is all terminate() does.
        if sys.platform == "win32":
            self.kill()
        else:
            import signal
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def kill(self) -> None:
        if self._job:
            _kernel32.TerminateJobObject(self._job, 1)
        elif sys.platform != "win32":
            import signal
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            self.process.kill()

    def close(self) -> None:
        if self._job:
            _kernel32.CloseHandle(self._job)
            self._job = None
