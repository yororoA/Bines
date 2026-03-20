import subprocess
import time
from typing import Iterable, List, Optional, Tuple


def _extract_listening_pid_by_ports(netstat_output: str, ports: List[int]) -> List[Tuple[int, str]]:
    matches: List[Tuple[int, str]] = []
    for line in netstat_output.splitlines():
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local_addr = parts[1]
        pid = parts[-1]
        if not (pid.isdigit() and pid != "0"):
            continue
        if ":" not in local_addr:
            continue
        try:
            port = int(local_addr.split(":")[-1])
        except (ValueError, IndexError):
            continue
        if port in ports:
            matches.append((port, pid))
    return matches


def cleanup_ports(
    ports: Iterable[int],
    *,
    initial_wait_seconds: float = 0.5,
    release_wait_seconds: float = 2.0,
    verify_retries: int = 3,
) -> None:
    """清理占用端口的进程（Windows: netstat + taskkill）。"""
    ports = [int(p) for p in ports if p is not None]
    if not ports:
        return

    print(f"🔍 检查并清理占用端口的进程: {ports}...")
    try:
        netstat = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        occupied = _extract_listening_pid_by_ports(netstat.stdout, ports)

        if not occupied:
            print("  ✅ 所有目标端口都可用，无需清理")
            time.sleep(initial_wait_seconds)
            return

        pids = sorted({pid for _, pid in occupied})
        for port, pid in occupied:
            print(f"  ⚠️  端口 {port} 被进程 PID {pid} 占用")

        print(f"  🔪 正在终止 {len(pids)} 个占用端口的进程...")
        for pid in pids:
            try:
                res = subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    print(f"    ✓ 已终止进程 PID {pid}")
                else:
                    err = (res.stderr or res.stdout or "").strip()
                    print(f"    ⚠️  终止进程 PID {pid} 失败: {err}")
                time.sleep(0.3)
            except subprocess.TimeoutExpired:
                print(f"    ⚠️  终止进程 PID {pid} 超时")
            except Exception as e:
                print(f"    ⚠️  终止进程 PID {pid} 失败: {e}")

        print("  ⏳ 等待端口释放...")
        time.sleep(release_wait_seconds)

        still_occupied: Optional[List[Tuple[int, str]]] = None
        for retry in range(max(1, int(verify_retries))):
            verify = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            still_occupied = _extract_listening_pid_by_ports(verify.stdout, ports)
            if not still_occupied:
                print("  ✅ 所有端口已成功释放")
                return
            if retry < verify_retries - 1:
                brief = [f"{p}(PID:{pid})" for p, pid in still_occupied]
                print(f"  ⚠️  仍有端口被占用: {brief}，等待后重试...")
                time.sleep(1.0)

        brief = [f"{p}(PID:{pid})" for p, pid in (still_occupied or [])]
        print(f"  ⚠️  警告: 以下端口可能仍被占用: {brief}")
    except Exception as e:
        print(f"  ❌ 端口清理过程中出错: {e}")
