"""Cooperative service shutdown, with a bounded process-exit deadline."""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

log = logging.getLogger("bines.shutdown")
SHUTDOWN_GRACE_SECONDS = 8.0


def join_threads(threads, timeout=SHUTDOWN_GRACE_SECONDS):
    """Share one deadline across workers instead of waiting once per worker."""
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(timeout=max(0, deadline - time.monotonic()))


def _exit_on_deadline(name, seconds, code):
    try:
        os.write(2, (f"{name} shutdown deadline exceeded after {seconds:.1f}s; "
                     f"in-flight work or cleanup did not finish; exiting {code}\n").encode())
    finally:
        os._exit(code)


def _exit_after_deadline(name, seconds, code):
    threading.Event().wait(seconds)
    # Executor atexit hooks can outlive a daemon service thread. This last
    # resort applies only to standalone entrypoints after an explicit signal.
    _exit_on_deadline(name, seconds, code)


def run_service(target, *, name, stop=None, grace=SHUTDOWN_GRACE_SECONDS, force_exit=False):
    """Run target(stop), handling TERM/INT without cleanup inside a handler.

    Service work and its ordinary children are daemons so a long RPC cannot
    hold the main thread. Standalone entrypoints also cover executor atexit
    waits; a deadline exit keeps signal status 143/130, never reports success.
    """
    stop = stop if stop is not None else threading.Event()
    received = []
    errors, result = [], []

    def request_stop(signum, _frame):
        received.append(signum)
        stop.set()

    def execute():
        try:
            result.append(target(stop))
        except BaseException as exc:
            errors.append(exc)

    signals = (signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    worker = threading.Thread(target=execute, name=name, daemon=True)
    try:
        for sig in signals:
            signal.signal(sig, request_stop)
        worker.start()
        while worker.is_alive() and not stop.wait(0.1):
            pass
        if stop.is_set():
            if force_exit and received:
                # Leave this guard armed until process exit, including atexit.
                threading.Thread(target=_exit_after_deadline,
                                 args=(name, grace, 128 + received[0]), daemon=True,
                                 name=name + "-shutdown-deadline").start()
            log.info("%s stopping · waiting up to %.1fs for cleanup", name, grace)
            worker.join(timeout=grace)
            if worker.is_alive():
                if force_exit and received:
                    _exit_on_deadline(name, grace, 128 + received[0])
                raise TimeoutError(f"{name} shutdown exceeded {grace}s")
        if errors:
            raise errors[0]
        return result[0] if result else None
    finally:
        stop.set()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
