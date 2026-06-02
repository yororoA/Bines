import threading

_global_cancel_event = threading.Event()
_thread_events: dict[str, threading.Event] = {}
_thread_events_lock = threading.Lock()


def get_cancel_event() -> threading.Event:
    return _global_cancel_event


def get_thread_cancel_event(thread_id: str) -> threading.Event:
    with _thread_events_lock:
        if thread_id not in _thread_events:
            _thread_events[thread_id] = threading.Event()
        return _thread_events[thread_id]


def cancel_thread(thread_id: str) -> None:
    with _thread_events_lock:
        event = _thread_events.get(thread_id)
        if event is not None:
            event.set()


def cancel_all_threads() -> None:
    with _thread_events_lock:
        for event in _thread_events.values():
            event.set()


def clear_thread_cancel_event(thread_id: str) -> None:
    with _thread_events_lock:
        event = _thread_events.get(thread_id)
        if event is not None:
            event.clear()


def remove_thread_cancel_event(thread_id: str) -> None:
    with _thread_events_lock:
        _thread_events.pop(thread_id, None)
