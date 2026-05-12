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

    print(f"[PortCleanup] Checking and cleaning occupied ports: {ports}...")
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
            print("  [OK] All target ports are free, no cleanup needed.")
            time.sleep(initial_wait_seconds)
            return

        pids = sorted({pid for _, pid in occupied})
        for port, pid in occupied:
            print(f"  [WARN] Port {port} is occupied by PID {pid}.")

        print(f"  [INFO] Terminating {len(pids)} process(es) occupying ports...")
        for pid in pids:
            try:
                res = subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    print(f"    [OK] Terminated PID {pid}.")
                else:
                    err = (res.stderr or res.stdout or "").strip()
                    print(f"    [WARN] Failed to terminate PID {pid}: {err}")
                time.sleep(0.3)
            except subprocess.TimeoutExpired:
                print(f"    [WARN] Timeout when terminating PID {pid}.")
            except Exception as e:
                print(f"    [WARN] Failed to terminate PID {pid}: {e}")

        print("  [INFO] Waiting for ports to be released...")
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
                print("  [OK] All ports released successfully.")
                return
            if retry < verify_retries - 1:
                brief = [f"{p}(PID:{pid})" for p, pid in still_occupied]
                print(f"  [WARN] Ports still occupied: {brief}, retrying after wait...")
                time.sleep(1.0)

        brief = [f"{p}(PID:{pid})" for p, pid in (still_occupied or [])]
        print(f"  [WARN] These ports may still be occupied: {brief}")
    except Exception as e:
        print(f"  [ERROR] Port cleanup failed: {e}")
