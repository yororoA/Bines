import threading

_cancel_event = threading.Event()


def get_cancel_event() -> threading.Event:
    return _cancel_event
