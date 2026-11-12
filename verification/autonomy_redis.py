"""Exercise a real disposable Redis: migration, races, AOF crash and outage.

Run: BINES_ENV_FILE=/dev/null python -m verification.autonomy_redis
Requires Docker and the redis Python client; uses no live Bines data/accounts.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from common.autonomy_store import AutonomyStore, AutonomyStoreError, prepare_redis
from common.activity import activity_control
from verification.autonomy_database import AutonomyDatabaseTests


def main():
    image = os.getenv("BINES_TEST_REDIS_IMAGE", "docker.m.daocloud.io/library/redis:7.4-alpine")
    container = "bines-autonomy-test-" + str(time.time_ns())
    volume = container + "-data"

    def docker(*args):
        result = subprocess.run(["docker", *args], check=True, text=True, capture_output=True)
        return result.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="bines-redis-test-") as temp:
        directory = Path(temp)
        config = prepare_redis(directory)
        user = f"{os.getuid() if hasattr(os, 'getuid') else 1000}:{os.getgid() if hasattr(os, 'getgid') else 1000}"
        try:
            docker("volume", "create", volume)
            mount = f"type=volume,source={volume},target=/data,volume-nocopy"
            docker("run", "--rm", "--user", "0:0", "--mount", mount, "--entrypoint", "sh",
                   image, "-c", f"chown {user} /data && chmod 700 /data")
            docker("run", "-d", "--name", container, "--user", user,
                   "-p", "127.0.0.1::6379", "-v", f"{config}:/etc/redis.conf:ro",
                   "--mount", mount, image, "redis-server", "/etc/redis.conf")
            port = docker("port", container, "6379/tcp").rsplit(":", 1)[1]
            url = f"redis://127.0.0.1:{port}/0"
            password = json.loads((directory / "autonomy_redis.json").read_text())["password"]
            from urllib.parse import quote
            test_url = f"redis://:{quote(password)}@127.0.0.1:{port}/0"
            states = AutonomyStore(directory=directory, backend="redis", url=url)

            def ready():
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        if states.redis.ping():
                            return
                    except Exception:
                        pass
                    threading.Event().wait(0.1)
                raise AssertionError("isolated Redis did not become ready")

            ready()
            with patch.dict(os.environ, {
                "BINES_TEST_AUTONOMY_BACKEND": "redis", "BINES_AUTONOMY_REDIS_URL": test_url,
            }):
                result = unittest.TextTestRunner(verbosity=1).run(
                    unittest.defaultTestLoader.loadTestsFromTestCase(AutonomyDatabaseTests))
            if not result.wasSuccessful():
                raise AssertionError("Redis behavior tests failed")
            states.put("concurrent", {"values": []}, 0)

            def append(number):
                states.update("concurrent", lambda old: {"values": old["values"] + [number]})

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(append, range(16)))
            assert sorted(states.get("concurrent")[0]["values"]) == list(range(16))
            print("Redis 并发原子更新：16 条交接均保留", flush=True)
            states.put("control", {"mode": "pause", "reason": "test"}, 0)
            expected = {
                "contact_wait_until": time.time() + 900, "next_decision": time.time() + 60,
                "weights": {"10": 0.4}, "interests": [{"topic": "test"}],
                "pending": {"id": "pending-do-not-replay"},
                "intention_queue": [{"kind": "curiosity", "intent": "another independent goal", "created_at": time.time()}],
                "next_intention_at": time.time() + 60,
                "waiting_activities": {"goal": {"note": "checkpoint"}},
            }
            states.put("desktop", expected, 0)
            with states.scheduler("crash-lease"):
                docker("kill", container)
                with patch.dict(os.environ, {
                    "BINES_MEMORY_DIR": temp, "BINES_AUTONOMY_BACKEND": "redis",
                    "BINES_AUTONOMY_REDIS_URL": url,
                }):
                    try:
                        activity_control()
                    except AutonomyStoreError as exc:
                        assert password not in str(exc)
                    else:
                        raise AssertionError("offline control must not imply resume")
                try:
                    states.put("desktop", {}, 1)
                except AutonomyStoreError:
                    pass
                else:
                    raise AssertionError("offline state must not be written elsewhere")
            print("Redis 断连：控制读取和自主写入均停止，凭据未进入错误日志", flush=True)
            docker("start", container)
            # Docker can assign a new host port for an anonymous port binding.
            port = docker("port", container, "6379/tcp").rsplit(":", 1)[1]
            url = f"redis://127.0.0.1:{port}/0"
            states = AutonomyStore(directory=directory, backend="redis", url=url)
            ready()
            restored = AutonomyStore(directory=directory, backend="redis", url=url)
            assert restored.get("desktop")[0] == expected
            assert restored.get("control")[0]["mode"] == "pause"
            assert sorted(restored.get("concurrent")[0]["values"]) == list(range(16))
            print("Redis 强制退出后的 AOF 恢复：冷却、兴趣、权重、意图队列、生成时钟、进度、暂停均一致", flush=True)
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


if __name__ == "__main__":
    main()
