"""ChatBot gateway; embeds lightweight memory only in the two-process QQ profile."""
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]
from chatBot.napcat_client import NapCatClient
from chatBot.store import MemoryStore
from chatBot.capabilities import CapabilityService
from chatBot.gateway import GatewayServer
from chatBot.memory_hub import MemoryHub


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Docker named-volume initialization can lag one scheduling tick behind the
    # dependency; retry the writable check instead of crash-looping the gateway.
    deadline = time.monotonic() + 30
    while True:
        try:
            store = MemoryStore()
            break
        except sqlite3.OperationalError as exc:
            if "unable to open database file" not in str(exc) or time.monotonic() >= deadline:
                raise
            logging.warning("gateway.db 暂不可写，1 秒后重试：%s", exc)
            time.sleep(1)
    client = NapCatClient()
    memory = MemoryHub(store)
    memory.start()
    client.add_event_listener(store.ingest)
    server = GatewayServer(CapabilityService(client, store), memory_hub=memory)
    client.connect()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from common.service_ready import mark_ready
    mark_ready("ChatBot", {"port": server.server_port})
    logging.info("ChatBot gateway ready · port %s", server.server_port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        client.disconnect()
        memory.close()


if __name__ == "__main__":
    main()
