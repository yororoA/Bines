"""Shared local PUB/SUB transport for the Python services.

Publishers connect to the bus ingress; subscribers connect to its egress.
Messages remain compatible with the existing two-frame ``[topic, JSON]`` format.
"""

import zmq

from config import ZMQ_HOST, ZMQ_PORTS


def publisher_endpoint() -> str:
    return f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['BUS_PUBLISH']}"


def subscriber_endpoint() -> str:
    return f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['BUS_SUBSCRIBE']}"


def create_publisher(context: zmq.Context, *, label: str = "publisher") -> zmq.Socket:
    """Create a PUB socket connected to the shared bus ingress."""
    socket = context.socket(zmq.PUB)
    socket.setsockopt(zmq.LINGER, 1000)
    socket.connect(publisher_endpoint())
    return socket


def create_subscriber(context: zmq.Context, *topics: str, label: str = "subscriber") -> zmq.Socket:
    """Create a SUB socket connected to the shared bus egress."""
    socket = context.socket(zmq.SUB)
    socket.setsockopt(zmq.LINGER, 1000)
    socket.connect(subscriber_endpoint())
    for topic in topics:
        socket.setsockopt_string(zmq.SUBSCRIBE, topic)
    return socket
