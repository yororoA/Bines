"""Forward all local module PUB/SUB traffic through one broker process."""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import zmq

from common.zmq_bus import publisher_endpoint, subscriber_endpoint


def main() -> int:
    context = zmq.Context()
    publishers = context.socket(zmq.XSUB)
    subscribers = context.socket(zmq.XPUB)
    publishers.setsockopt(zmq.LINGER, 0)
    subscribers.setsockopt(zmq.LINGER, 0)

    try:
        publishers.bind(publisher_endpoint())
        subscribers.bind(subscriber_endpoint())
        print(
            f"[ZMQ Bus] ready: PUB ingress={publisher_endpoint()}, "
            f"SUB egress={subscriber_endpoint()}",
            flush=True,
        )
        from common.service_ready import mark_ready
        mark_ready("ZMQ Bus")
        zmq.proxy(publishers, subscribers)
    except KeyboardInterrupt:
        print("[ZMQ Bus] stopping", flush=True)
    except zmq.ZMQError as exc:
        print(f"[ZMQ Bus] fatal: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        publishers.close(linger=0)
        subscribers.close(linger=0)
        context.term()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
