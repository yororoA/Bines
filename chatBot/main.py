"""ChatBot gateway; no ZMQ, camera, audio or model dependency."""
import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chatBot.napcat_client import NapCatClient
from chatBot.store import MemoryStore
from chatBot.capabilities import CapabilityService
from chatBot.gateway import GatewayServer


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store, client = MemoryStore(), NapCatClient()
    client.add_event_listener(store.ingest)
    server = GatewayServer(CapabilityService(client, store))
    client.connect()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("ChatBot gateway ready · port %s", server.server_port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        client.disconnect()


if __name__ == "__main__":
    main()
