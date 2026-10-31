"""Per-installation service credentials shared through the private data directory."""
from __future__ import annotations

import hmac
import os
from pathlib import Path
import secrets
import time

from common.env import load_env


def token_matches(supplied, expected: str) -> bool:
    # JSON may contain Unicode or lone escaped surrogates; str compare_digest
    # only supports ASCII and must not make an invalid credential kill a server.
    return isinstance(supplied, str) and hmac.compare_digest(
        supplied.encode("utf-8", errors="surrogatepass"), expected.encode("utf-8", errors="surrogatepass"))


def service_token(variable: str, filename: str) -> str:
    """Use an explicit credential or create a private, concurrent-start-safe file."""
    load_env()
    configured = os.environ.get(variable, "").strip()
    if configured:
        return configured
    root = Path(__file__).resolve().parents[1]
    directory = Path(os.environ.get("BINES_MEMORY_DIR") or root / "thingking" / "memory_data")
    if not directory.is_absolute():
        directory = root / directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32))
            stream.flush()
            os.fsync(stream.fileno())
    # A different service may have created the file but not completed its write.
    for _ in range(100):
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
        time.sleep(0.01)
    raise RuntimeError(f"{filename} is empty; restore the service credential before starting")


def memory_token() -> str:
    return service_token("BINES_MEMORY_RPC_TOKEN", "memory-rpc.token")


def admin_token() -> str:
    return service_token("BINES_ADMIN_TOKEN", "admin.token")
