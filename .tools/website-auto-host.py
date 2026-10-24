"""Restart the local bridge from private credentials without argv secrets."""
import os
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.chdir(root)
from common.website_spool import read_bridge_token
from website_runtime import (
    installed_chrome_for_testing, ensure_agent_browser, AGENT_BROWSER_SOCKET_DIR,
    AGENT_BROWSER_RUNTIME_HOME, AGENT_BROWSER_CRASH_DIR,
)

spool = root / "thingking/memory_data/website_bridge"
token = read_bridge_token(spool)
assert token, "bridge credential unavailable"
os.environ.update(
    BINES_AGENT_BROWSER_COMMAND=__import__("shlex").join(ensure_agent_browser()),
    BINES_CHROME_FOR_TESTING_EXECUTABLE=installed_chrome_for_testing(),
    AGENT_BROWSER_SOCKET_DIR=str(AGENT_BROWSER_SOCKET_DIR),
    HOME=str(AGENT_BROWSER_RUNTIME_HOME),
    BINES_WEBSITE_AUTO_OBSERVE="false",
    BINES_WEBSITE_PAUSED="true",
    AGENT_BROWSER_ARGS=",".join([
        "--disable-breakpad", "--disable-crash-reporter", "--no-first-run",
        "--no-default-browser-check", "--crash-dumps-dir=" + str(AGENT_BROWSER_CRASH_DIR),
    ]),
)
sys.argv = [
    str(root / "website_bridge.py"), "--host", "127.0.0.1", "--port", "49468",
    "--token=" + token, "--spool", str(spool), "--parent-pid", "35872",
]
log = (root / "logs/website-bridge.log").open("a", buffering=1)
os.dup2(log.fileno(), 1)
os.dup2(log.fileno(), 2)
runpy.run_path(str(root / "website_bridge.py"), run_name="__main__")
